"""Where runs and their steps are written. In-memory for tests and --memory, Postgres for real.

Every write to a run or a step also refreshes runs.updated_at: that is the heartbeat the stale
sweep reads."""

import uuid
from datetime import UTC, datetime
from typing import Any, Protocol

from psycopg.types.json import Jsonb

from grocery_planner.llm.types import Usage
from grocery_planner.orchestrator.models import RunRecord, StepRecord

_RUN_FIELDS = {
    "status", "stop_reason", "failure_reason", "user_message", "ingredient_output",
    "price_output", "planner_output", "models_used", "fell_back", "requests", "tokens_in",
    "tokens_out", "thinking_tokens", "latency_ms", "loopback_used", "completed_at",
    "langfuse_trace_id",
}  # fmt: skip
_RUN_JSON = {"ingredient_output", "price_output", "planner_output"}


class RunStore(Protocol):
    def create_run(
        self, request: dict[str, Any], parameters: dict[str, Any], estimated_requests: int
    ) -> str: ...

    def update_run(self, run_id: str, **fields: Any) -> None: ...

    def start_step(
        self, run_id: str, name: str, agent: str | None, attempt: int, input: Any
    ) -> int: ...

    def finish_step(
        self,
        run_id: str,
        seq: int,
        *,
        status: str,
        output: Any = None,
        tool_calls: list[Any] | None = None,
        usage: Usage | None = None,
        model: str | None = None,
        fell_back: bool = False,
        latency_ms: int | None = None,
        summary: str | None = None,
        error: str | None = None,
    ) -> None: ...

    def get_run(self, run_id: str) -> RunRecord: ...

    def get_steps(self, run_id: str) -> list[StepRecord]: ...

    def running_not_updated_since(self, cutoff: datetime) -> list[str]: ...


def _now() -> datetime:
    return datetime.now(UTC)


class InMemoryRunStore:
    def __init__(self) -> None:
        self.runs: dict[str, RunRecord] = {}
        self.steps: dict[str, list[StepRecord]] = {}

    def create_run(
        self, request: dict[str, Any], parameters: dict[str, Any], estimated_requests: int
    ) -> str:
        run_id = str(uuid.uuid4())
        now = _now()
        self.runs[run_id] = RunRecord(
            id=run_id, input=request, parameters=parameters,
            estimated_requests=estimated_requests, created_at=now, updated_at=now,
        )  # fmt: skip
        self.steps[run_id] = []
        return run_id

    def update_run(self, run_id: str, **fields: Any) -> None:
        unknown = set(fields) - _RUN_FIELDS
        if unknown:
            raise ValueError(f"unknown run fields: {sorted(unknown)}")
        self.runs[run_id] = self.runs[run_id].model_copy(update={**fields, "updated_at": _now()})

    def start_step(
        self, run_id: str, name: str, agent: str | None, attempt: int, input: Any
    ) -> int:
        seq = len(self.steps[run_id]) + 1
        self.steps[run_id].append(
            StepRecord(
                seq=seq, name=name, agent=agent, attempt=attempt, input=input, started_at=_now()
            )
        )
        self.update_run(run_id)
        return seq

    def finish_step(
        self,
        run_id: str,
        seq: int,
        *,
        status: str,
        output: Any = None,
        tool_calls: list[Any] | None = None,
        usage: Usage | None = None,
        model: str | None = None,
        fell_back: bool = False,
        latency_ms: int | None = None,
        summary: str | None = None,
        error: str | None = None,
    ) -> None:
        u = usage or Usage()
        step = self.steps[run_id][seq - 1]
        self.steps[run_id][seq - 1] = step.model_copy(
            update=dict(
                status=status, output=output, tool_calls=tool_calls or [], requests=u.requests,
                tokens_in=u.input_tokens, tokens_out=u.output_tokens,
                thinking_tokens=u.thinking_tokens, model=model or None, fell_back=fell_back,
                latency_ms=latency_ms, summary=summary, error=error, finished_at=_now(),
            )
        )  # fmt: skip
        self.update_run(run_id)

    def get_run(self, run_id: str) -> RunRecord:
        return self.runs[run_id]

    def get_steps(self, run_id: str) -> list[StepRecord]:
        return list(self.steps[run_id])

    def running_not_updated_since(self, cutoff: datetime) -> list[str]:
        return [r.id for r in self.runs.values() if r.status == "running" and r.updated_at < cutoff]


class PostgresRunStore:
    """runs and run_steps through a small connection pool (migrations 001 to 004)."""

    def __init__(self, dsn: str, min_size: int = 1, max_size: int = 4) -> None:
        from psycopg_pool import ConnectionPool

        from grocery_planner.db import CONNECT_KWARGS

        self._pool = ConnectionPool(
            dsn, min_size=min_size, max_size=max_size, kwargs=CONNECT_KWARGS, open=True
        )
        self._pool.wait()

    def close(self) -> None:
        self._pool.close()

    def create_run(
        self, request: dict[str, Any], parameters: dict[str, Any], estimated_requests: int
    ) -> str:
        with self._pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO runs (input, parameters, estimated_requests) "
                "VALUES (%s, %s, %s) RETURNING id",
                (Jsonb(request), Jsonb(parameters), estimated_requests),
            ).fetchone()
        return str(row["id"])

    def update_run(self, run_id: str, **fields: Any) -> None:
        unknown = set(fields) - _RUN_FIELDS
        if unknown:
            raise ValueError(f"unknown run fields: {sorted(unknown)}")
        values = [Jsonb(v) if k in _RUN_JSON and v is not None else v for k, v in fields.items()]
        sets = ", ".join([*(f"{k} = %s" for k in fields), "updated_at = now()"])
        with self._pool.connection() as conn:
            conn.execute(f"UPDATE runs SET {sets} WHERE id = %s", (*values, run_id))

    def start_step(
        self, run_id: str, name: str, agent: str | None, attempt: int, input: Any
    ) -> int:
        with self._pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO run_steps (run_id, seq, name, agent, attempt, input) "
                "VALUES (%s, (SELECT COALESCE(MAX(seq), 0) + 1 FROM run_steps WHERE run_id = %s), "
                "%s, %s, %s, %s) RETURNING seq",
                (run_id, run_id, name, agent, attempt, Jsonb(input)),
            ).fetchone()
            conn.execute("UPDATE runs SET updated_at = now() WHERE id = %s", (run_id,))
        return row["seq"]

    def finish_step(
        self,
        run_id: str,
        seq: int,
        *,
        status: str,
        output: Any = None,
        tool_calls: list[Any] | None = None,
        usage: Usage | None = None,
        model: str | None = None,
        fell_back: bool = False,
        latency_ms: int | None = None,
        summary: str | None = None,
        error: str | None = None,
    ) -> None:
        u = usage or Usage()
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE run_steps SET status = %s, output = %s, tool_calls = %s, requests = %s, "
                "tokens_in = %s, tokens_out = %s, thinking_tokens = %s, model = %s, "
                "fell_back = %s, latency_ms = %s, summary = %s, error = %s, finished_at = now() "
                "WHERE run_id = %s AND seq = %s",
                (
                    status, Jsonb(output) if output is not None else None,
                    Jsonb(tool_calls or []), u.requests, u.input_tokens, u.output_tokens,
                    u.thinking_tokens, model or None, fell_back, latency_ms, summary, error,
                    run_id, seq,
                ),
            )  # fmt: skip
            conn.execute("UPDATE runs SET updated_at = now() WHERE id = %s", (run_id,))

    def get_run(self, run_id: str) -> RunRecord:
        with self._pool.connection() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = %s", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        row["id"] = str(row["id"])
        row["models_used"] = list(row["models_used"] or [])
        return RunRecord.model_validate(row)

    def get_steps(self, run_id: str) -> list[StepRecord]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM run_steps WHERE run_id = %s ORDER BY seq", (run_id,)
            ).fetchall()
        return [StepRecord.model_validate({**r, "tool_calls": r["tool_calls"] or []}) for r in rows]

    def running_not_updated_since(self, cutoff: datetime) -> list[str]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT id FROM runs WHERE status = 'running' AND updated_at < %s", (cutoff,)
            ).fetchall()
        return [str(r["id"]) for r in rows]
