"""Needs a real Postgres (SUPABASE_DB_URL). Skipped otherwise. Note: the fixture upserts the
seed catalogue with today's anchor, which is the same state the daily seed job produces."""

from datetime import date

import psycopg
import pytest

from grocery_planner.config import ACTIVE_STORES
from grocery_planner.db import CONNECT_KWARGS, db_url
from grocery_planner.db.migrate import migrate
from grocery_planner.db.seed import seed
from grocery_planner.tools.price_lookup import (
    PostgresPriceSource,
    SeededFilePriceSource,
    today_sydney,
)

pytestmark = pytest.mark.integration

TERMS = [
    "bacon", "eye fillet", "beef mince", "chicken breast", "chicken", "milk", "eggs", "egg",
    "bread", "white bread", "pasta", "spaghetti", "tomato sauce", "tomato", "cheese", "rice",
    "oil", "yoghurt", "sourdough", "potato gems", "dragonfruit", "fries", "peas", "onion",
]  # fmt: skip
BRANDS = [None, "Kingsway", "bellvue", "Coles", "Nope"]


@pytest.fixture(scope="module")
def anchor() -> date:
    return today_sydney()


@pytest.fixture(scope="module")
def pg(anchor):
    migrate(db_url())
    seed(db_url(), anchor)
    source = PostgresPriceSource(db_url())
    yield source
    source.close()


def test_postgres_matches_seeded_source(pg, anchor):
    seeded = SeededFilePriceSource(anchor=anchor)
    checked = 0
    for store in ACTIVE_STORES:
        for term in TERMS:
            for brand in BRANDS:
                for limit in (1, 50):
                    assert pg.search(term, store, limit, brand) == seeded.search(
                        term, store, limit, brand
                    ), (term, store, brand, limit)
                    checked += 1
    assert checked == len(ACTIVE_STORES) * len(TERMS) * len(BRANDS) * 2


def test_seed_is_idempotent(anchor):
    assert seed(db_url(), anchor) == seed(db_url(), anchor)


def test_migrations_are_idempotent():
    migrate(db_url())
    assert migrate(db_url()) == []  # second run applies nothing, no error


def test_rls_enabled_on_every_table_with_no_policies():
    with psycopg.connect(db_url(), **CONNECT_KWARGS) as conn:
        tables = conn.execute(
            "SELECT tablename, rowsecurity FROM pg_tables WHERE schemaname = 'public'"
        ).fetchall()
        policies = conn.execute(
            "SELECT tablename, policyname FROM pg_policies WHERE schemaname = 'public'"
        ).fetchall()
    names = {t["tablename"] for t in tables}
    assert {"runs", "run_steps", "price_catalogue", "dish_cache", "usage_counters"} <= names
    assert [t["tablename"] for t in tables if not t["rowsecurity"]] == []
    assert policies == []


def test_dish_cache_round_trip():
    from decimal import Decimal

    from grocery_planner.db.dish_cache import PostgresDishCache
    from grocery_planner.domain.ingredients import DishIngredient, DishResult

    key = ("integration test dish", "dairy,nuts", "test-v1")
    result = DishResult(
        ingredients=[
            DishIngredient(
                name="butter", quantity_per_serving=Decimal("12.5"), unit="g", in_vocabulary=True
            )
        ],
        assumptions=["Swapped for oil."],
    )
    cache = PostgresDishCache(db_url())
    try:
        assert cache.get(*key) is None
        cache.put(*key, "Integration Test Dish", result, "model-a")
        assert cache.get(*key) == result
        assert cache.get(key[0], "", key[2]) is None  # a different avoid is a different row
        assert cache.get(key[0], key[1], "test-v2") is None  # a different prompt version misses
        cache.put(*key, "Integration Test Dish", result.model_copy(update={"assumptions": []}), "b")
        assert cache.get(*key).assumptions == []  # upsert overwrites
    finally:
        with psycopg.connect(db_url(), **CONNECT_KWARGS) as conn:
            conn.execute("DELETE FROM dish_cache WHERE dish_key = %s", (key[0],))
        cache.close()


def test_answering_model_columns_exist():
    with psycopg.connect(db_url(), **CONNECT_KWARGS) as conn:
        cols = {
            (r["table_name"], r["column_name"]): r["data_type"]
            for r in conn.execute(
                "SELECT table_name, column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name IN ('runs', 'run_steps')"
            )
        }
    assert cols[("run_steps", "model")] == "text"
    assert cols[("run_steps", "fell_back")] == "boolean"
    assert cols[("runs", "models_used")] == "ARRAY"


def test_orchestrator_writes_runs_and_steps_to_postgres_and_replays_events():
    from datetime import UTC, datetime, timedelta

    from grocery_planner.db.dish_cache import InMemoryDishCache
    from grocery_planner.domain import PlannerConfig
    from grocery_planner.llm import FakeLLMClient
    from grocery_planner.orchestrator import PostgresRunStore, Runner
    from tests.test_orchestrator import AS_OF, REQUEST, SOURCE, happy_script, loopback_script

    migrate(db_url())
    store = PostgresRunStore(db_url())
    run_ids = []
    try:
        for script in (happy_script, loopback_script):
            runner = Runner(
                store, lambda hooks, s=script: FakeLLMClient(s(), on_request=hooks.on_request),
                InMemoryDishCache(), SOURCE,
            )  # fmt: skip
            run_id = runner.create_run(REQUEST, PlannerConfig(), AS_OF)
            run_ids.append(run_id)
            assert store.get_run(run_id).status == "queued"
            row = runner.run_plan(run_id)
            assert row.status == "completed" and row.models_used == ["fake-model"]
            steps = store.get_steps(run_id)
            assert [s.name for s in steps] == (
                ["ingredient", "price", "planner"]
                if script is happy_script
                else ["ingredient", "price", "planner", "reprice", "planner_2"]
            )
            assert all(s.status == "completed" and s.model == "fake-model" for s in steps)
            assert row.requests == len(steps) and row.loopback_used == (script is loopback_script)
            assert row.planner_output["summary_line"].startswith("No meaningful difference")
            events = runner.events(run_id)
            assert runner.events(run_id, after_seq=3) == events[3:]
            assert events[-1].type == "run_completed"
        # the stale sweep, against real timestamps
        stale = store.create_run({}, {}, 9)
        run_ids.append(stale)
        store.update_run(stale, status="running")
        with psycopg.connect(db_url(), **CONNECT_KWARGS) as conn:
            conn.execute(
                "UPDATE runs SET updated_at = now() - interval '6 minutes' WHERE id = %s", (stale,)
            )
        runner = Runner(store, lambda hooks: None, InMemoryDishCache(), SOURCE)
        assert stale in runner.sweep_stale(datetime.now(UTC) + timedelta(seconds=1))
        assert store.get_run(stale).failure_reason == "stale"
    finally:
        with psycopg.connect(db_url(), **CONNECT_KWARGS) as conn:
            conn.execute("DELETE FROM runs WHERE id = ANY(%s::uuid[])", (run_ids,))
        store.close()


def test_the_runner_reserves_and_settles_the_budget_in_postgres():
    from datetime import UTC, datetime

    from grocery_planner.db.dish_cache import InMemoryDishCache
    from grocery_planner.domain import PlannerConfig
    from grocery_planner.guardrails import PostgresDayBudget
    from grocery_planner.llm import FakeLLMClient
    from grocery_planner.orchestrator import PostgresRunStore, Runner
    from tests.test_orchestrator import AS_OF, REQUEST, SOURCE, happy_script

    migrate(db_url())
    store = PostgresRunStore(db_url())
    day_now = datetime(2099, 6, 1, 18, 0, tzinfo=UTC)
    budget = PostgresDayBudget(db_url(), "itest-primary", None, now=lambda: day_now, ip_secret="k")
    run_ids = []

    def make(hooks):
        return FakeLLMClient(
            happy_script(), on_request=hooks.on_request, before_request=hooks.before_request,
            model="itest-primary",
        )  # fmt: skip

    try:
        runner = Runner(store, make, InMemoryDishCache(), SOURCE, budget=budget)
        run_id = runner.create_run(REQUEST, PlannerConfig(), AS_OF)
        run_ids.append(run_id)
        row = runner.run_plan(run_id)
        assert row.status == "completed"
        (u,) = budget.snapshot()
        assert (u.used, u.reserved) == (3, 0)
        with psycopg.connect(db_url(), **CONNECT_KWARGS) as conn:
            r = conn.execute(
                "SELECT reserved_remaining, reservation_day FROM runs WHERE id = %s", (run_id,)
            ).fetchone()
        assert r["reserved_remaining"] == 0 and str(r["reservation_day"]) == "2099-06-01"
    finally:
        with psycopg.connect(db_url(), **CONNECT_KWARGS) as conn:
            conn.execute("DELETE FROM usage_counters WHERE day >= '2099-01-01'")
            conn.execute("DELETE FROM runs WHERE id = ANY(%s::uuid[])", (run_ids,))
        budget.close()
        store.close()
