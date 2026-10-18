"""Per-agent timeout for one model request (not for the whole step).

Each agent wraps its model call in `with agent_timeout("planner"):`; the Gemini client reads the
value when it builds a request and falls back to its own default outside any agent (the probe,
ad hoc calls). The numbers come from measured latency (see docs/DECISIONS.md, step 15b): about
2 to 3 times the p95 of one call, never under 8 s and never over 25 s. Retry and fallback rules
are not touched: a timeout is still retried once and then falls back.

Override with GEMINI_TIMEOUT_<AGENT>_S (seconds), for example GEMINI_TIMEOUT_PLANNER_S=18. An
override outside the floor and cap is clamped, so a typo cannot make every call time out."""

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

log = logging.getLogger(__name__)

FLOOR_MS = 8_000
CAP_MS = 25_000
DEFAULT_TIMEOUT_MS = 20_000  # outside any agent
AGENT_TIMEOUT_MS: dict[str, int] = {"ingredient": 12_000, "price": 25_000, "planner": 25_000}

_current: ContextVar[int | None] = ContextVar("llm_request_timeout_ms", default=None)


def timeout_ms_for(agent: str) -> int:
    """The per-request timeout for an agent: the environment override, else the default."""
    raw = os.environ.get(f"GEMINI_TIMEOUT_{agent.upper()}_S", "").strip()
    if raw:
        try:
            value = round(float(raw) * 1000)
        except ValueError:
            log.warning("ignoring GEMINI_TIMEOUT_%s_S=%r (not a number)", agent.upper(), raw)
        else:
            clamped = min(CAP_MS, max(FLOOR_MS, value))
            if clamped != value:
                log.warning("GEMINI_TIMEOUT_%s_S clamped to %d ms", agent.upper(), clamped)
            return clamped
    return AGENT_TIMEOUT_MS.get(agent, DEFAULT_TIMEOUT_MS)


@contextmanager
def agent_timeout(agent: str) -> Iterator[None]:
    token = _current.set(timeout_ms_for(agent))
    try:
        yield
    finally:
        _current.reset(token)


def current_timeout_ms(default: int = DEFAULT_TIMEOUT_MS) -> int:
    value = _current.get()
    return default if value is None else value
