"""The usage counter against a real Postgres (SUPABASE_DB_URL; skipped otherwise). Rows are made
with a random hash and deleted again, and the stats are compared as before/after differences so
the test also works on a database that already holds real data."""

import secrets
from datetime import datetime

import psycopg
import pytest

from grocery_planner.db import CONNECT_KWARGS, db_url
from grocery_planner.db.migrate import migrate
from grocery_planner.db.visitors import SYDNEY, PostgresVisitors
from grocery_planner.orchestrator import PostgresRunStore

pytestmark = pytest.mark.integration


@pytest.fixture
def repo():
    migrate(db_url())
    visitors = PostgresVisitors(db_url())
    yield visitors
    visitors.close()


def delta(before: dict, after: dict) -> dict:
    statuses = before["runs_by_status"]
    return {
        "total_runs": after["total_runs"] - before["total_runs"],
        "runs_by_status": {k: after["runs_by_status"][k] - statuses[k] for k in statuses},
        "today": after["runs_per_day"][-1]["runs"] - before["runs_per_day"][-1]["runs"],
        "unique_visitors": after["unique_visitors"] - before["unique_visitors"],
        "returning_visitors": after["returning_visitors"] - before["returning_visitors"],
    }


def test_visitors_table_has_no_link_to_runs_and_has_rls():
    migrate(db_url())
    with psycopg.connect(db_url(), **CONNECT_KWARGS) as conn:
        columns = {
            r["column_name"]
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'visitors'"
            )
        }
        rls = conn.execute(
            "SELECT rowsecurity FROM pg_tables WHERE tablename = 'visitors'"
        ).fetchone()
        links = conn.execute(
            "SELECT 1 FROM information_schema.table_constraints c "
            "JOIN information_schema.constraint_column_usage u USING (constraint_name) "
            "WHERE c.table_name = 'visitors' AND c.constraint_type = 'FOREIGN KEY'"
        ).fetchall()
    assert columns == {"visitor_hash", "first_seen", "last_seen", "run_count", "is_owner"}
    assert rls["rowsecurity"] is True and links == []


def test_record_run_upserts_and_stats_leave_out_the_owner(repo):
    mine, other, owner = (secrets.token_hex(16) for _ in range(3))
    store = PostgresRunStore(db_url())
    run_ids = []
    try:
        before = repo.stats(datetime.now(SYDNEY).date())
        repo.record_run(mine, owner=False)
        first = row(mine)
        repo.record_run(mine, owner=False)
        repo.record_run(other, owner=False)
        repo.record_run(owner, owner=True)
        repo.record_run(owner, owner=False)  # the mark sticks
        second = row(mine)
        assert (first["run_count"], second["run_count"]) == (1, 2)
        assert (
            second["first_seen"] == first["first_seen"]
            and second["last_seen"] >= first["last_seen"]
        )
        assert row(owner)["is_owner"] is True and row(owner)["run_count"] == 2
        for status, is_owner in (("completed", False), ("failed", False), ("completed", True)):
            run_id = store.create_run({}, {}, 0)
            run_ids.append(run_id)
            store.update_run(run_id, status=status, is_owner=is_owner)
        assert store.get_run(run_ids[2]).is_owner is True
        after = repo.stats(datetime.now(SYDNEY).date())
        assert len(after["runs_per_day"]) == 30
        assert delta(before, after) == {
            "total_runs": 2,
            "runs_by_status": {"completed": 1, "stopped": 0, "failed": 1},
            "today": 2,
            "unique_visitors": 2,  # mine and other; the owner is left out
            "returning_visitors": 1,  # mine has two runs
        }
    finally:
        with psycopg.connect(db_url(), **CONNECT_KWARGS) as conn:
            conn.execute(
                "DELETE FROM visitors WHERE visitor_hash = ANY(%s)", ([mine, other, owner],)
            )
            if run_ids:
                conn.execute("DELETE FROM runs WHERE id = ANY(%s::uuid[])", (run_ids,))
        store.close()


def row(visitor_hash: str) -> dict:
    with psycopg.connect(db_url(), **CONNECT_KWARGS) as conn:
        return conn.execute(
            "SELECT * FROM visitors WHERE visitor_hash = %s", (visitor_hash,)
        ).fetchone()
