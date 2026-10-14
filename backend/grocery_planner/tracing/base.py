"""Tracing interface. The orchestrator and the model layer talk to a Tracer; which one is behind
it (Langfuse, a recording fake, or nothing) is decided at start-up. Tracing must never break or
slow a run, so the orchestrator always wraps whatever it is given in SafeTracer."""

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Protocol

from grocery_planner.llm.types import (
    GenerationHandle,
    GenerationStart,
    NoopGenerationHandle,
    ToolCallRecord,
)

log = logging.getLogger(__name__)
APP_ENVS = ("dev", "eval", "prod")


def current_app_env() -> str:
    """dev, eval or prod, from APP_ENV (case-insensitive); anything else counts as dev."""
    from grocery_planner.env import getenv

    value = (getenv("APP_ENV") or "dev").strip().lower()
    if value not in APP_ENVS:
        log.warning("APP_ENV=%r is not one of %s; using dev", value, APP_ENVS)
        return "dev"
    return value


@dataclass(frozen=True)
class RunTraceInfo:
    run_id: str
    app_env: str
    tags: list[str]
    metadata: dict[str, Any]  # as_of, meals, dishes, config used. Never a visitor id or an IP.
    input: Any = None


class StepTrace(Protocol):
    def start_generation(self, info: GenerationStart) -> GenerationHandle: ...

    def tool_call(self, record: ToolCallRecord) -> None: ...

    def end(
        self,
        *,
        output: Any = None,
        metadata: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None: ...


class RunTrace(Protocol):
    trace_id: str | None

    def start_step(
        self, name: str, *, prompt_ref: str | None, attempt: int, input: Any = None
    ) -> StepTrace: ...

    def finish(
        self, *, status: str, output: Any = None, metadata: dict[str, Any] | None = None
    ) -> None: ...


class Tracer(Protocol):
    def start_run(self, info: RunTraceInfo) -> RunTrace: ...

    def trace_url(self, trace_id: str) -> str | None: ...


# ---- the no-op tracer (default in tests, and when there are no keys) --------------------------


class NoopStep:
    def start_generation(self, info: GenerationStart) -> GenerationHandle:
        return NoopGenerationHandle()

    def tool_call(self, record: ToolCallRecord) -> None:
        pass

    def end(self, *, output=None, metadata=None, error=None) -> None:
        pass


@dataclass
class NoopRun:
    trace_id: str | None = None

    def start_step(self, name, *, prompt_ref, attempt, input=None) -> StepTrace:
        return NoopStep()

    def finish(self, *, status, output=None, metadata=None) -> None:
        pass


class NoopTracer:
    enabled = False

    def start_run(self, info: RunTraceInfo) -> RunTrace:
        return NoopRun()

    def trace_url(self, trace_id: str) -> str | None:
        return None


# ---- the safety wrapper -----------------------------------------------------------------------

_logged: set[tuple[str, str]] = set()


def _swallow(owner: object, method: str, exc: Exception) -> None:
    key = (type(owner).__name__, method)
    first = key not in _logged
    _logged.add(key)
    (log.warning if first else log.debug)(
        "tracing error in %s.%s (ignored)", key[0], method, exc_info=first
    )


class _SafeHandle:
    def __init__(self, inner: GenerationHandle) -> None:
        self._inner = inner

    def end(self, *, output=None, usage=None, error=None) -> None:
        try:
            self._inner.end(output=output, usage=usage, error=error)
        except Exception as exc:
            _swallow(self._inner, "end", exc)


class _SafeStep:
    def __init__(self, inner: StepTrace) -> None:
        self._inner = inner

    def start_generation(self, info: GenerationStart) -> GenerationHandle:
        try:
            return _SafeHandle(self._inner.start_generation(info))
        except Exception as exc:
            _swallow(self._inner, "start_generation", exc)
            return NoopGenerationHandle()

    def tool_call(self, record: ToolCallRecord) -> None:
        try:
            self._inner.tool_call(record)
        except Exception as exc:
            _swallow(self._inner, "tool_call", exc)

    def end(self, *, output=None, metadata=None, error=None) -> None:
        try:
            self._inner.end(output=output, metadata=metadata, error=error)
        except Exception as exc:
            _swallow(self._inner, "end", exc)


class _SafeRun:
    def __init__(self, inner: RunTrace) -> None:
        self._inner = inner

    @property
    def trace_id(self) -> str | None:
        try:
            return self._inner.trace_id
        except Exception as exc:
            _swallow(self._inner, "trace_id", exc)
            return None

    def start_step(self, name, *, prompt_ref, attempt, input=None) -> StepTrace:
        try:
            return _SafeStep(
                self._inner.start_step(name, prompt_ref=prompt_ref, attempt=attempt, input=input)
            )
        except Exception as exc:
            _swallow(self._inner, "start_step", exc)
            return NoopStep()

    def finish(self, *, status, output=None, metadata=None) -> None:
        try:
            self._inner.finish(status=status, output=output, metadata=metadata)
        except Exception as exc:
            _swallow(self._inner, "finish", exc)


@dataclass
class SafeTracer:
    """Catches and logs every error from the wrapped tracer, at every level."""

    inner: Tracer
    enabled: bool = field(init=False, default=True)

    def __post_init__(self) -> None:
        self.enabled = getattr(self.inner, "enabled", True)

    def start_run(self, info: RunTraceInfo) -> RunTrace:
        try:
            return _SafeRun(self.inner.start_run(info))
        except Exception as exc:
            _swallow(self.inner, "start_run", exc)
            return NoopRun()

    def trace_url(self, trace_id: str) -> str | None:
        try:
            return self.inner.trace_url(trace_id)
        except Exception as exc:
            _swallow(self.inner, "trace_url", exc)
            return None


def tracing_disabled_by_env() -> bool:
    return os.environ.get("LANGFUSE_ENABLED", "").strip().lower() in ("0", "false", "no", "off")
