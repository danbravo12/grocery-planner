"""The model client for evals, and the meter that counts what a case used.

Evals use their own API key (GEMINI_EVAL_API_KEY, never the demo's), a pinned model with the
fallback switched off, and the real per-minute limiter settings (12/min) with a longer wait so a
sequential run queues instead of failing.
"""

import os
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from grocery_planner.env import getenv
from grocery_planner.llm import GeminiClient, ModelHooks
from grocery_planner.llm.gemini import DEFAULT_MODEL
from grocery_planner.llm.ratelimit import RateLimiter
from grocery_planner.llm.types import (
    GenerationHandle,
    GenerationStart,
    LLMResult,
    NoopGenerationHandle,
    Usage,
)
from grocery_planner.tracing import would_cost

ANCHOR_ISO = "2026-10-12"  # the fixed as_of every eval uses (a Monday); seed dates resolve to it
EVAL_LIMITER = RateLimiter(per_minute=12, max_wait=180.0)


class EvalConfigError(RuntimeError):
    """The environment is not set up for evals (exit code 2)."""


class EvalQuotaExhausted(RuntimeError):
    """Google says the eval key's daily quota is gone. The run stops there; nothing after it is
    scored, because every later case would fail for a reason that says nothing about the model."""


def prepare_environment() -> None:
    """APP_ENV=eval tags traces; the eval key must exist and differ from the demo key."""
    os.environ["APP_ENV"] = "eval"
    key = getenv("GEMINI_EVAL_API_KEY")
    if not key:
        raise EvalConfigError(
            "GEMINI_EVAL_API_KEY is not set. Evals never use GEMINI_API_KEY, so CI cannot use up "
            "the demo's daily budget."
        )
    if key == getenv("GEMINI_API_KEY"):
        raise EvalConfigError("GEMINI_EVAL_API_KEY must be a different key from GEMINI_API_KEY.")


def resolve_model(model: str | None) -> str:
    return model or getenv("GEMINI_MODEL") or DEFAULT_MODEL


def scrub(text: str) -> str:
    """Never let a key into a results file."""
    for name in ("GEMINI_EVAL_API_KEY", "GEMINI_API_KEY"):
        key = getenv(name)
        if key:
            text = text.replace(key, "<redacted>")
    return text


@dataclass
class Meter:
    """What one case used.

    Model behaviour is counted as model calls: first calls, tool rounds and repairs (a send with
    attempt 1). Transport retries (5xx, timeouts, per-minute 429: attempt 2 and later) are
    infrastructure: reported, never a reason to fail a case. `calls` leaves repairs out so that
    a case limit says how many questions the agent asked; repairs are reported next to it.
    """

    calls: int = 0  # first sends that were a request or a tool round
    repairs: int = 0  # first sends that were a schema repair
    transport_retries: int = 0
    sends: int = 0  # every request that went out, retries included
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    would_cost_usd: Decimal = Decimal(0)
    fell_back: bool = False
    models: list[str] = field(default_factory=list)

    @property
    def model_calls(self) -> int:
        return self.calls + self.repairs

    def start_generation(self, info: GenerationStart) -> GenerationHandle:
        if info.attempt > 1:
            self.transport_retries += 1
        elif info.kind == "repair":
            self.repairs += 1
        else:
            self.calls += 1
        return NoopGenerationHandle()

    def on_request(self, usage: Usage) -> None:
        self.sends += usage.requests
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.thinking_tokens += usage.thinking_tokens
        cost = would_cost(usage.model, usage)
        if cost is not None:
            self.would_cost_usd += cost.total
        if usage.model and usage.model not in self.models:
            self.models.append(usage.model)

    def on_result(self, result: LLMResult) -> None:
        self.fell_back = self.fell_back or result.fell_back


class MeteredLLM:
    """Wraps an LLMClient and records each successful call's repair and fallback flags."""

    def __init__(self, inner: Any, meter: Meter) -> None:
        self._inner, self._meter = inner, meter

    def generate(self, *args: Any, **kwargs: Any):
        result = self._inner.generate(*args, **kwargs)
        self._meter.on_result(result)
        return result

    def generate_with_tools(self, *args: Any, **kwargs: Any):
        result = self._inner.generate_with_tools(*args, **kwargs)
        self._meter.on_result(result)
        return result


def make_client(model: str, meter: Meter, hooks: ModelHooks | None = None) -> MeteredLLM:
    hooks = hooks or ModelHooks()

    def on_request(usage: Usage) -> None:
        meter.on_request(usage)
        if hooks.on_request:
            hooks.on_request(usage)

    def start_generation(info: GenerationStart) -> GenerationHandle:
        meter.start_generation(info)
        return hooks.start_generation(info) if hooks.start_generation else NoopGenerationHandle()

    client = GeminiClient(
        api_key=getenv("GEMINI_EVAL_API_KEY"),
        model=model,
        fallback_model=None,  # pinned: a failure must surface, not be answered by another model
        rate_limiter=EVAL_LIMITER,
        on_request=on_request,
        before_request=hooks.before_request,
        fallback_allowed=hooks.fallback_allowed,
        on_quota_exhausted=hooks.on_quota_exhausted,
        start_generation=start_generation,
        on_tool_call=hooks.on_tool_call,
    )
    return MeteredLLM(client, meter)
