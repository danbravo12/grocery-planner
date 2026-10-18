"""Provider-neutral types for the model layer. Nothing here imports a provider SDK."""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ValidationError
from pydantic_core import to_jsonable_python

# ---- errors ---------------------------------------------------------------------------------


class LLMError(Exception):
    """Base class for model-layer failures."""


class LLMUnavailable(LLMError):
    """Timeouts, 5xx or per-minute throttling persisted through every retry."""


class QuotaExhausted(LLMError):
    def __init__(
        self, kind: Literal["per_minute", "per_day", "billing", "unknown"], message: str = ""
    ) -> None:
        super().__init__(f"quota exhausted ({kind}){': ' + message if message else ''}")
        self.kind = kind


class BudgetExhausted(QuotaExhausted):
    """Our own daily request budget for a model is used up (not Google's quota). It behaves like
    a per-day quota error, so the model layer falls back to the other model when it can."""

    def __init__(self, model: str, message: str = "") -> None:
        super().__init__("per_day", message or f"daily request budget used up for {model}")
        self.model = model


class SchemaValidationFailed(LLMError):
    def __init__(self, raw_outputs: list[str], errors: list[str]) -> None:
        super().__init__("model output failed schema validation after one repair attempt")
        self.raw_outputs = raw_outputs
        self.errors = errors


class ToolLoopExceeded(LLMError):
    pass


# ---- usage and results ----------------------------------------------------------------------


@dataclass(frozen=True)
class Usage:
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0  # excludes thinking tokens
    thinking_tokens: int = 0
    # The model that served the request(s). Per-request deltas carry one model; a sum over
    # several models says "mixed". Not part of equality.
    model: str | None = field(default=None, compare=False)

    def __add__(self, other: "Usage") -> "Usage":
        if self.model is None or self.model == other.model:
            model = other.model if self.model is None else self.model
        elif other.model is None:
            model = self.model
        else:
            model = "mixed"
        return Usage(
            self.requests + other.requests,
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.thinking_tokens + other.thinking_tokens,
            model,
        )


OnRequest = Callable[[Usage], None]  # called once per request actually sent, with its delta


@dataclass(frozen=True)
class GenerationStart:
    """What a tracer is told when a request is about to be sent."""

    model: str
    attempt: int  # 1 for the first send of this request, 2+ for retries
    kind: str  # request | repair | tool_round
    fell_back: bool
    system: str
    user: str  # the user payload of the call
    last_turn: str | None  # the newest turn when it is not the user payload (repair, tool result)
    turns: int
    temperature: float | None = None


class GenerationHandle(Protocol):
    def end(
        self, *, output: Any = None, usage: "Usage | None" = None, error: str | None = None
    ) -> None: ...


class NoopGenerationHandle:
    def end(self, *, output: Any = None, usage: "Usage | None" = None, error: str | None = None):
        pass


@dataclass
class ModelHooks:
    """What the budget and tracing layers plug into a model client."""

    on_request: OnRequest | None = None  # after every request actually sent
    before_request: Callable[[str], None] | None = None  # (model) before a send; may raise
    fallback_allowed: Callable[[], bool] | None = None  # False: act as if no fallback is set
    on_quota_exhausted: Callable[[str, str], None] | None = (
        None  # (model, kind: per_day or billing) from Google
    )
    start_generation: Callable[[GenerationStart], GenerationHandle] | None = None  # tracing
    on_tool_call: Callable[["ToolCallRecord"], None] | None = None  # tracing


@dataclass(frozen=True)
class ToolCallRecord:
    name: str
    args: dict[str, Any]
    result_summary: str
    latency_ms: float
    error: str | None = None


@dataclass(frozen=True)
class LLMResult[T: BaseModel]:
    parsed: T
    model: str  # the model that actually answered
    attempts: int  # requests sent for this call: first tries, retries, repairs, tool rounds
    repair_used: bool
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    latency_ms: int = 0
    fell_back: bool = False  # the primary model was unavailable or out of daily quota
    fallback_reason: str | None = None


# ---- tools ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    """A pure local function the model may ask for. Our code validates the args and runs it."""

    name: str
    description: str
    input_model: type[BaseModel]
    func: Callable[..., Any]  # called as func(**validated_args)


class LLMClient(Protocol):
    def generate[T: BaseModel](
        self, system: str, user: str, output_type: type[T], temperature: float = 0.1
    ) -> LLMResult[T]: ...

    def generate_with_tools[T: BaseModel](
        self,
        system: str,
        user: str,
        output_type: type[T],
        tools: list[ToolSpec],
        max_tool_rounds: int,
    ) -> LLMResult[T]: ...


def summarize(value: Any, limit: int = 200) -> str:
    text = json.dumps(value, default=str)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def execute_tool(
    tools: dict[str, ToolSpec], name: str, args: dict[str, Any], clock: Callable[[], float]
) -> tuple[dict[str, Any], ToolCallRecord]:
    """Validate args, run the tool, and build the response to send back to the model.
    Unknown tools, invalid args and ValueErrors raised by the tool come back as error results."""
    start = clock()

    def done(response: dict[str, Any], summary: str, error: str | None = None):
        record = ToolCallRecord(name, args, summary, round((clock() - start) * 1000, 3), error)
        return response, record

    spec = tools.get(name)
    if spec is None:
        msg = f"unknown tool '{name}'; available tools: {', '.join(sorted(tools))}"
        return done({"error": msg}, msg, msg)
    try:
        validated = spec.input_model.model_validate(args)
    except ValidationError as e:
        msg = f"invalid arguments for '{name}': {e.errors(include_url=False, include_input=False)}"
        return done({"error": msg}, msg[:200], msg)
    try:
        result = to_jsonable_python(spec.func(**validated.model_dump()))
    except ValueError as e:
        return done({"error": str(e)}, f"error: {e}", str(e))
    return done({"result": result}, summarize(result))
