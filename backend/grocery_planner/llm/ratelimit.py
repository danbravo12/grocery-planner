"""A per-model requests-per-minute limiter, in process (a known single-process limitation)."""

import threading
import time
from collections import deque
from collections.abc import Callable

from grocery_planner.config import GUARDRAILS
from grocery_planner.llm.types import LLMUnavailable


class RateLimiter:
    """Sliding one-minute window per model. `acquire` takes a slot, waiting for one if the model
    is at its limit; if no slot would open within `max_wait` seconds it raises LLMUnavailable."""

    def __init__(
        self,
        per_minute: int = GUARDRAILS.requests_per_minute,
        max_wait: float = GUARDRAILS.rate_limit_max_wait_seconds,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.per_minute, self.max_wait = per_minute, max_wait
        self._clock, self._sleep = clock, sleep
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def acquire(self, model: str) -> None:
        start = self._clock()
        while True:
            with self._lock:
                now = self._clock()
                hits = self._hits.setdefault(model, deque())
                while hits and hits[0] <= now - 60:
                    hits.popleft()
                if len(hits) < self.per_minute:
                    hits.append(now)
                    return
                wait = hits[0] + 60 - now
            if (now - start) + wait > self.max_wait:
                raise LLMUnavailable(
                    f"rate limit: no request slot for {model} within {self.max_wait:g}s"
                )
            self._sleep(wait)


# Shared by every real client in the process, so concurrent runs share one budget per minute.
DEFAULT_RATE_LIMITER = RateLimiter()
