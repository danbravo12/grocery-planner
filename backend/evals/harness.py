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
from grocery_planner.llm.types import LLMResult, Usage
from grocery_planner.tracing import would_cost

ANCHOR_ISO = "2026-10-12"  # the fixed as_of every eval uses (a Monday); seed dates resolve to it
EVAL_LIMITER = RateLimiter(per_minute=12, max_wait=180.0)


class EvalConfigError(RuntimeError):
    """The environment is not set up for evals (exit code 2)."""


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
    """What one case used. Requests and tokens come from the per-request hook (so failed sends
    count); repairs and fallbacks come from the results of the calls that succeeded."""

    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    would_cost_usd: Decimal = Decimal(0)
    repairs: int = 0
    fell_back: bool = False
    models: list[str] = field(default_factory=list)

    def on_request(self, usage: Usage) -> None:
        self.requests += usage.requests
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.thinking_tokens += usage.thinking_tokens
        cost = would_cost(usage.model, usage)
        if cost is not None:
            self.would_cost_usd += cost.total
        if usage.model and usage.model not in self.models:
            self.models.append(usage.model)

    def on_result(self, result: LLMResult) -> None:
        self.repairs += int(result.repair_used)
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

    client = GeminiClient(
        api_key=getenv("GEMINI_EVAL_API_KEY"),
        model=model,
        fallback_model=None,  # pinned: a failure must surface, not be answered by another model
        rate_limiter=EVAL_LIMITER,
        on_request=on_request,
        before_request=hooks.before_request,
        fallback_allowed=hooks.fallback_allowed,
        on_quota_exhausted=hooks.on_quota_exhausted,
        start_generation=hooks.start_generation,
        on_tool_call=hooks.on_tool_call,
    )
    return MeteredLLM(client, meter)
