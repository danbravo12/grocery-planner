"""The usage counter: unique browsers and runs, without knowing who anyone is.

A visitor is the HMAC-SHA256 of the visitor cookie id under VISITOR_HASH_SECRET. The table
(migration 008) keeps that hash and four numbers; it has no run_id and no IP, and the raw cookie
id is never written anywhere. Owner visits are marked so the stats can leave them out. The
numbers are "unique browsers", not people: clearing cookies or using another device makes a new one.
"""

import hashlib
import hmac
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from grocery_planner.orchestrator.store import InMemoryRunStore

SYDNEY = ZoneInfo("Australia/Sydney")
STATS_DAYS = 30
RETURNING_RUNS = 2  # a returning visitor has started at least this many runs
STATUSES = ("completed", "stopped", "failed")


def visitor_hash(secret: str, visitor_id: str) -> str:
    """HMAC-SHA256 of the cookie id: stable for the same secret, useless without it."""
    return hmac.new(secret.encode(), visitor_id.encode(), hashlib.sha256).hexdigest()


class VisitorRepo(Protocol):
    def record_run(self, visitor_hash: str, owner: bool) -> None:
        """An admitted run: set last_seen, add one to run_count, remember an owner visit."""
        ...

    def stats(self, today: date) -> dict[str, Any]:
        """Totals for the usage endpoint, all leaving out the owner's visits and runs."""
        ...


def _stats(
    by_status: dict[str, int], per_day: dict[date, int], unique: int, returning: int, today: date
) -> dict[str, Any]:
    days = [today - timedelta(days=n) for n in range(STATS_DAYS - 1, -1, -1)]
    return {
        "total_runs": sum(by_status.values()),
        "runs_by_status": {s: by_status.get(s, 0) for s in STATUSES},
        "runs_per_day": [{"day": d.isoformat(), "runs": per_day.get(d, 0)} for d in days],
        "unique_visitors": unique,
        "returning_visitors": returning,
    }


class InMemoryVisitors:
    """The same rules as PostgresVisitors in one process (tests and API_STORE=memory). Run
    totals are read from the in-memory run store."""

    def __init__(
        self,
        runs: "InMemoryRunStore | None" = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self._runs, self._now = runs, now

    def record_run(self, visitor_hash: str, owner: bool) -> None:
        now = self._now()
        row = self.rows.setdefault(
            visitor_hash,
            {"first_seen": now, "last_seen": now, "run_count": 0, "is_owner": False},
        )
        row["last_seen"] = now
        row["run_count"] += 1
        row["is_owner"] = row["is_owner"] or owner

    def stats(self, today: date) -> dict[str, Any]:
        by_status: dict[str, int] = {}
        per_day: dict[date, int] = {}
        for run in self._runs.runs.values() if self._runs else ():
            if run.is_owner:
                continue
            by_status[run.status] = by_status.get(run.status, 0) + 1
            day = run.created_at.astimezone(SYDNEY).date()
            per_day[day] = per_day.get(day, 0) + 1
        counts = [r["run_count"] for r in self.rows.values() if not r["is_owner"]]
        returning = sum(1 for n in counts if n >= RETURNING_RUNS)
        return _stats(by_status, per_day, len(counts), returning, today)


class PostgresVisitors:
    """The visitors table (migration 008) and the runs table, through a small pool."""

    def __init__(self, dsn: str, min_size: int = 1, max_size: int = 2) -> None:
        from psycopg_pool import ConnectionPool

        from grocery_planner.db import CONNECT_KWARGS

        self._pool = ConnectionPool(
            dsn, min_size=min_size, max_size=max_size, kwargs=CONNECT_KWARGS, open=True
        )
        self._pool.wait()

    def close(self) -> None:
        self._pool.close()

    def record_run(self, visitor_hash: str, owner: bool) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO visitors (visitor_hash, run_count, is_owner) VALUES (%s, 1, %s) "
                "ON CONFLICT (visitor_hash) DO UPDATE SET last_seen = now(), "
                "run_count = visitors.run_count + 1, "
                "is_owner = visitors.is_owner OR EXCLUDED.is_owner",
                (visitor_hash, owner),
            )

    def stats(self, today: date) -> dict[str, Any]:
        first_day = today - timedelta(days=STATS_DAYS - 1)
        since = datetime.combine(first_day, datetime.min.time(), SYDNEY)
        with self._pool.connection() as conn:
            statuses = conn.execute(
                "SELECT status, count(*) AS n FROM runs WHERE NOT is_owner GROUP BY status"
            ).fetchall()
            days = conn.execute(
                "SELECT (created_at AT TIME ZONE 'Australia/Sydney')::date AS day, count(*) AS n "
                "FROM runs WHERE NOT is_owner AND created_at >= %s GROUP BY 1",
                (since,),
            ).fetchall()
            visitors = conn.execute(
                "SELECT count(*) AS n, count(*) FILTER (WHERE run_count >= %s) AS returning "
                "FROM visitors WHERE NOT is_owner",
                (RETURNING_RUNS,),
            ).fetchone()
        return _stats(
            {r["status"]: r["n"] for r in statuses},
            {r["day"]: r["n"] for r in days},
            visitors["n"],
            visitors["returning"],
            today,
        )
