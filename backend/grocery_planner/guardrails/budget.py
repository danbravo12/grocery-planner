"""The daily request budget and the per-visitor limits.

Counters live in usage_counters (see migration 005), one row per (Pacific day, model). Requests
are what Google's free tier limits, so requests are what we count. Two limits per model:

  admission_limit  a NEW run is admitted only if used + reserved + its worst-case estimate fits
  daily_limit      the most a model may be asked for, including runs already in flight

The gap between them is the buffer that lets runs that have started finish.
"""

import hashlib
import logging
import os
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from grocery_planner.config import GUARDRAILS, GuardrailConfig
from grocery_planner.guardrails.clock import Availability, build_availability, pacific_day
from grocery_planner.llm.types import BudgetExhausted
from grocery_planner.orchestrator.budget import BudgetUnavailable

log = logging.getLogger(__name__)
MIN_RUN_REQUESTS = 3  # fewest requests a run can need: ingredient, price, planner
STOP_DAY_BUDGET = "day_budget"  # our counter says no new runs fit
STOP_GOOGLE_QUOTA = "google_quota"  # Google returned a per-day 429 for this model


@dataclass(frozen=True)
class ModelUsage:
    model: str
    used: int
    reserved: int
    daily_limit: int
    admission_limit: int
    day_stopped: bool
    stop_reason: str | None


@dataclass(frozen=True)
class AdmitResult:
    ok: bool
    reason: Literal["visitor_limit", "ip_limit"] | None = None


class IpHasher:
    """sha256(ip + daily salt). The salt is derived from a server secret and the Pacific date,
    so it changes every day and is never stored alongside the counters."""

    def __init__(self, secret: str | None = None) -> None:
        secret = secret or os.environ.get("IP_HASH_SECRET")
        if not secret:
            log.warning("IP_HASH_SECRET is not set; using a random secret for this process")
            secret = secrets.token_hex(16)
        self._secret = secret

    def salt(self, day: date) -> str:
        return hashlib.sha256(f"{self._secret}:{day.isoformat()}".encode()).hexdigest()

    def hash(self, ip: str, day: date) -> str:
        return hashlib.sha256((ip + self.salt(day)).encode()).hexdigest()


def _flag_for(model: str, used: int, primary: str, cfg: GuardrailConfig) -> bool:
    """Should this model's day-stop flag be set after `used` requests?"""
    if model == primary:
        return used + MIN_RUN_REQUESTS > cfg.admission_limit  # cannot admit even a small run
    return used >= cfg.daily_limit  # the fallback is switched off when its budget is used up


class _Common:
    primary: str
    fallback: str | None
    cfg: GuardrailConfig
    _now: Callable[[], datetime]
    _ips: IpHasher

    def _today(self, now: datetime | None = None) -> date:
        return pacific_day(now or self._now())

    def availability(self, now: datetime | None = None) -> Availability:
        now = now or self._now()
        row = self._usage(self.primary, pacific_day(now))
        reason = None
        if row.day_stopped:
            reason = row.stop_reason or STOP_DAY_BUDGET
        elif row.used + MIN_RUN_REQUESTS > self.cfg.admission_limit:
            reason = STOP_DAY_BUDGET
        return build_availability(now, reason)

    def snapshot(self, now: datetime | None = None) -> list[ModelUsage]:
        day = pacific_day(now or self._now())
        models = [self.primary, *([self.fallback] if self.fallback else [])]
        return [self._usage(m, day) for m in models]

    def _usage(self, model: str, day: date) -> ModelUsage:  # pragma: no cover - abstract
        raise NotImplementedError

    def before_request(self, model: str) -> None:
        row = self._usage(model, self._today())
        if row.stop_reason == STOP_GOOGLE_QUOTA and row.day_stopped:
            raise BudgetExhausted(model, f"Google reports the daily quota for {model} is used up")
        if row.used >= self.cfg.daily_limit:
            raise BudgetExhausted(model)

    def prune(self, keep_days: int = 7, now: datetime | None = None) -> int:  # pragma: no cover
        """Delete counters older than `keep_days` Pacific days; returns how many rows went."""
        raise NotImplementedError

    def fallback_allowed(self) -> bool:
        if self.fallback is None:
            return False
        row = self._usage(self.fallback, self._today())
        return not row.day_stopped and row.used < self.cfg.daily_limit


class InMemoryDayBudget(_Common):
    """The same rules as PostgresDayBudget, in one process (tests and --memory)."""

    def __init__(
        self,
        primary: str,
        fallback: str | None = None,
        config: GuardrailConfig = GUARDRAILS,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        ip_secret: str | None = None,
    ) -> None:
        self.primary, self.fallback, self.cfg, self._now = primary, fallback, config, now
        self._ips = IpHasher(ip_secret)
        self._lock = threading.Lock()
        self._rows: dict[tuple[date, str], dict[str, Any]] = {}
        self._runs: dict[str, list] = {}  # run_id -> [remaining, reservation day]
        self.counters: dict[tuple[date, str, str], int] = {}  # visitor / ip runs per day

    def _row(self, day: date, model: str) -> dict[str, Any]:
        return self._rows.setdefault(
            (day, model), dict(used=0, reserved=0, stopped=False, reason=None)
        )

    def _usage(self, model: str, day: date) -> ModelUsage:
        with self._lock:
            r = self._row(day, model)
            return ModelUsage(
                model, r["used"], r["reserved"], self.cfg.daily_limit, self.cfg.admission_limit,
                r["stopped"], r["reason"],
            )  # fmt: skip

    def reserve(self, run_id: str, estimated_requests: int) -> None:
        day = self._today()
        with self._lock:
            r = self._row(day, self.primary)
            if r["stopped"]:
                raise BudgetUnavailable("day_budget")
            if r["used"] + r["reserved"] + estimated_requests <= self.cfg.admission_limit:
                r["reserved"] += estimated_requests
                self._runs[run_id] = [estimated_requests, day]
                return
            if r["used"] + estimated_requests > self.cfg.admission_limit:
                r["stopped"], r["reason"] = True, STOP_DAY_BUDGET
                raise BudgetUnavailable("day_budget")
        raise BudgetUnavailable("busy")

    def record(self, run_id: str, model: str | None, requests: int) -> None:
        if not model:
            return
        with self._lock:
            remaining, rday = self._runs.get(run_id, [0, None])
            covered = min(remaining, requests)
            if covered:
                self._runs[run_id][0] -= covered
                prow = self._row(rday, self.primary)
                prow["reserved"] = max(0, prow["reserved"] - covered)
            row = self._row(self._today(), model)
            row["used"] += requests
            if not row["stopped"] and _flag_for(model, row["used"], self.primary, self.cfg):
                row["stopped"], row["reason"] = True, STOP_DAY_BUDGET

    def release(self, run_id: str) -> None:
        with self._lock:
            remaining, rday = self._runs.get(run_id, [0, None])
            if remaining:
                prow = self._row(rday, self.primary)
                prow["reserved"] = max(0, prow["reserved"] - remaining)
                self._runs[run_id][0] = 0

    def quota_exhausted(self, model: str, kind: str) -> None:
        if kind == "per_day":
            with self._lock:
                row = self._row(self._today(), model)
                row["stopped"], row["reason"] = True, STOP_GOOGLE_QUOTA

    def prune(self, keep_days: int = 7, now: datetime | None = None) -> int:
        cutoff = self._today(now) - timedelta(days=keep_days)
        with self._lock:
            old_rows = [k for k in self._rows if k[0] < cutoff]
            old_counters = [k for k in self.counters if k[0] < cutoff]
            for k in old_rows:
                del self._rows[k]
            for k in old_counters:
                del self.counters[k]
        return len(old_rows) + len(old_counters)

    def admit(self, visitor_id: str, ip: str, now: datetime | None = None) -> AdmitResult:
        day = self._today(now)
        keys = [
            ((day, "visitor", visitor_id), self.cfg.visitor_runs_per_day, "visitor_limit"),
            ((day, "ip", self._ips.hash(ip, day)), self.cfg.ip_runs_per_day, "ip_limit"),
        ]
        with self._lock:
            for key, limit, reason in keys:
                if self.counters.get(key, 0) >= limit:
                    return AdmitResult(False, reason)
            for key, _, _ in keys:
                self.counters[key] = self.counters.get(key, 0) + 1
        return AdmitResult(True)


# The one statement that decides whether a run may start. Check and reserve happen together, in
# the database, so concurrent runs cannot both slip past the limit.
RESERVE_SQL = """
UPDATE usage_counters
   SET reserved = reserved + %(est)s, updated_at = now()
 WHERE day = %(day)s AND scope = 'global' AND scope_key = 'global' AND model = %(model)s
   AND NOT day_stopped
   AND used + reserved + %(est)s <= %(admission)s
RETURNING used, reserved
"""
_ENSURE_ROW = (
    "INSERT INTO usage_counters (day, scope, scope_key, model) "
    "VALUES (%s, 'global', 'global', %s) ON CONFLICT DO NOTHING"
)
_SELECT_ROW = (
    "SELECT used, reserved, day_stopped, stop_reason FROM usage_counters "
    "WHERE day = %s AND scope = 'global' AND scope_key = 'global' AND model = %s"
)
_STOP_ROW = (
    "UPDATE usage_counters SET day_stopped = true, stop_reason = %s, updated_at = now() "
    "WHERE day = %s AND scope = 'global' AND scope_key = 'global' AND model = %s"
)
# One increment that refuses to go past the limit: the row is only updated while used < limit.
ADMIT_SQL = """
INSERT INTO usage_counters (day, scope, scope_key, model, used)
VALUES (%(day)s, %(scope)s, %(key)s, '', 1)
ON CONFLICT (day, scope, scope_key, model)
DO UPDATE SET used = usage_counters.used + 1, updated_at = now()
WHERE usage_counters.used < %(limit)s
RETURNING used
"""


class PostgresDayBudget(_Common):
    def __init__(
        self,
        dsn: str,
        primary: str,
        fallback: str | None = None,
        config: GuardrailConfig = GUARDRAILS,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        ip_secret: str | None = None,
        max_size: int = 8,
    ) -> None:
        from psycopg_pool import ConnectionPool

        from grocery_planner.db import CONNECT_KWARGS

        self.primary, self.fallback, self.cfg, self._now = primary, fallback, config, now
        self._ips = IpHasher(ip_secret)
        self._pool = ConnectionPool(
            dsn, min_size=1, max_size=max_size, kwargs=CONNECT_KWARGS, open=True
        )
        self._pool.wait()

    def close(self) -> None:
        self._pool.close()

    def _usage(self, model: str, day: date) -> ModelUsage:
        with self._pool.connection() as conn:
            conn.execute(_ENSURE_ROW, (day, model))
            r = conn.execute(_SELECT_ROW, (day, model)).fetchone()
        return ModelUsage(
            model, r["used"], r["reserved"], self.cfg.daily_limit, self.cfg.admission_limit,
            r["day_stopped"], r["stop_reason"],
        )  # fmt: skip

    def reserve(self, run_id: str, estimated_requests: int) -> None:
        day = self._today()
        refusal: str | None = None
        with self._pool.connection() as conn:
            conn.execute(_ENSURE_ROW, (day, self.primary))
            params = dict(
                est=estimated_requests, day=day, model=self.primary,
                admission=self.cfg.admission_limit,
            )  # fmt: skip
            if conn.execute(RESERVE_SQL, params).fetchone():
                conn.execute(
                    "UPDATE runs SET reserved_remaining = %s, reservation_day = %s WHERE id = %s",
                    (estimated_requests, day, run_id),
                )
                return
            row = conn.execute(_SELECT_ROW, (day, self.primary)).fetchone()
            if row["day_stopped"]:
                refusal = "day_budget"
            elif row["used"] + estimated_requests > self.cfg.admission_limit:
                conn.execute(_STOP_ROW, (STOP_DAY_BUDGET, day, self.primary))
                refusal = "day_budget"
            else:
                refusal = "busy"  # only other runs' reservations are in the way
        raise BudgetUnavailable(refusal)  # outside the transaction, so the stop flag commits

    def record(self, run_id: str, model: str | None, requests: int) -> None:
        if not model:
            return
        day = self._today()
        with self._pool.connection() as conn:
            run = conn.execute(
                "SELECT reserved_remaining, reservation_day FROM runs WHERE id = %s FOR UPDATE",
                (run_id,),
            ).fetchone()
            covered = min(run["reserved_remaining"], requests) if run else 0
            if covered:
                conn.execute(
                    "UPDATE runs SET reserved_remaining = reserved_remaining - %s WHERE id = %s",
                    (covered, run_id),
                )
                conn.execute(
                    "UPDATE usage_counters SET reserved = GREATEST(reserved - %s, 0), "
                    "updated_at = now() WHERE day = %s AND scope = 'global' "
                    "AND scope_key = 'global' AND model = %s",
                    (covered, run["reservation_day"], self.primary),
                )
            used = conn.execute(
                "INSERT INTO usage_counters (day, scope, scope_key, model, used) "
                "VALUES (%s, 'global', 'global', %s, %s) "
                "ON CONFLICT (day, scope, scope_key, model) "
                "DO UPDATE SET used = usage_counters.used + EXCLUDED.used, updated_at = now() "
                "RETURNING used, day_stopped",
                (day, model, requests),
            ).fetchone()
            if not used["day_stopped"] and _flag_for(model, used["used"], self.primary, self.cfg):
                conn.execute(_STOP_ROW, (STOP_DAY_BUDGET, day, model))

    def release(self, run_id: str) -> None:
        with self._pool.connection() as conn:
            run = conn.execute(
                "SELECT reserved_remaining, reservation_day FROM runs WHERE id = %s FOR UPDATE",
                (run_id,),
            ).fetchone()
            if not run or not run["reserved_remaining"]:
                return
            conn.execute(
                "UPDATE usage_counters SET reserved = GREATEST(reserved - %s, 0), "
                "updated_at = now() WHERE day = %s AND scope = 'global' "
                "AND scope_key = 'global' AND model = %s",
                (run["reserved_remaining"], run["reservation_day"], self.primary),
            )
            conn.execute("UPDATE runs SET reserved_remaining = 0 WHERE id = %s", (run_id,))

    def quota_exhausted(self, model: str, kind: str) -> None:
        if kind != "per_day":
            return
        day = self._today()
        with self._pool.connection() as conn:
            conn.execute(_ENSURE_ROW, (day, model))
            conn.execute(_STOP_ROW, (STOP_GOOGLE_QUOTA, day, model))

    def prune(self, keep_days: int = 7, now: datetime | None = None) -> int:
        cutoff = self._today(now) - timedelta(days=keep_days)
        with self._pool.connection() as conn:
            cur = conn.execute("DELETE FROM usage_counters WHERE day < %s", (cutoff,))
            return cur.rowcount

    def admit(self, visitor_id: str, ip: str, now: datetime | None = None) -> AdmitResult:
        day = self._today(now)
        plan = [
            ("visitor", visitor_id, self.cfg.visitor_runs_per_day, "visitor_limit"),
            ("ip", self._ips.hash(ip, day), self.cfg.ip_runs_per_day, "ip_limit"),
        ]
        refused: str | None = None
        with self._pool.connection() as conn:
            for scope, key, limit, reason in plan:
                got = conn.execute(
                    ADMIT_SQL, dict(day=day, scope=scope, key=key, limit=limit)
                ).fetchone()
                if got is None:
                    refused = reason
                    conn.rollback()  # undo the visitor increment if the IP limit refused
                    break
        return AdmitResult(refused is None, refused)  # type: ignore[arg-type]
