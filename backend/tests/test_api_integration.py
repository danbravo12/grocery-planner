"""One request through the API against the real Postgres store and budget (fake models).
Needs SUPABASE_DB_URL; skipped otherwise. Counters use a far-future day and are cleaned up."""

from datetime import UTC, date, datetime

import psycopg
import pytest
from fastapi.testclient import TestClient

from grocery_planner.api import ApiSettings, create_app
from grocery_planner.api.app import Services
from grocery_planner.db import CONNECT_KWARGS, db_url
from grocery_planner.db.dish_cache import InMemoryDishCache
from grocery_planner.db.migrate import migrate
from grocery_planner.guardrails import PostgresDayBudget
from grocery_planner.orchestrator import PostgresRunStore, Runner
from tests.test_api import BODY, FAST, SmartFake, wait_for
from tests.test_orchestrator import AS_OF, SOURCE

pytestmark = pytest.mark.integration
DAY = date(2099, 6, 1)


def test_one_post_against_postgres_with_fake_models(monkeypatch):
    monkeypatch.setattr("grocery_planner.api.app.today_sydney", lambda: AS_OF)
    url = db_url()
    migrate(url)  # a fresh CI database has no tables yet
    store = PostgresRunStore(url)
    budget = PostgresDayBudget(
        url, "fake-model", None, now=lambda: datetime(2099, 6, 1, 20, tzinfo=UTC), ip_secret="x"
    )
    runner = Runner(
        store,
        lambda hooks: SmartFake(
            [], on_request=hooks.on_request, before_request=hooks.before_request
        ),
        InMemoryDishCache(),
        SOURCE,
        budget=budget,
    )
    app = create_app(ApiSettings(**FAST), Services(runner, budget))
    run_id = None
    try:
        with TestClient(app) as client:
            r = client.post("/api/runs", json=BODY)
            assert r.status_code == 202
            run_id = r.json()["run_id"]
            wait_for(lambda: client.get(f"/api/runs/{run_id}").json()["status"] == "completed", 20)
            view = client.get(f"/api/runs/{run_id}").json()
            steps = client.get(f"/api/runs/{run_id}/steps").json()["steps"]
            events = client.get(f"/api/runs/{run_id}/events").text  # finished: replays and closes
        assert view["recommendation"]["recommendation"] == "no_meaningful_difference"
        assert view["totals"]["requests"] == 3 and len(view["comparison"]) == 2
        assert [s["name"] for s in steps] == ["ingredient", "price", "planner"]
        assert events.count("event: ") == 7 and "event: run_completed" in events
        assert "langfuse" not in str(view) and "reserved" not in str(view)
        with psycopg.connect(url, **CONNECT_KWARGS) as conn:
            counters = {
                (c["scope"], c["model"]): (c["used"], c["reserved"])
                for c in conn.execute("SELECT * FROM usage_counters WHERE day = %s", (DAY,))
            }
            assert counters[("global", "fake-model")] == (3, 0)  # used 3, reservation released
            assert sum(1 for k in counters if k[0] in ("visitor", "ip")) == 2
            left = conn.execute(
                "SELECT reserved_remaining FROM runs WHERE id = %s", (run_id,)
            ).fetchone()
            assert left["reserved_remaining"] == 0
        # a refused request leaves no row behind
        unknown = client.get("/api/runs/00000000-0000-4000-8000-000000000000")
        assert unknown.status_code == 404
    finally:
        with psycopg.connect(url, **CONNECT_KWARGS) as conn:
            if run_id:
                conn.execute("DELETE FROM runs WHERE id = %s", (run_id,))
            conn.execute("DELETE FROM usage_counters WHERE day = %s", (DAY,))
        store.close()
        budget.close()
