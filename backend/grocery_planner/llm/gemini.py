"""LLMClient on Google's google-genai SDK (generate_content API).

Hand-rolled on purpose: every request is visible and counted. The SDK's automatic function
calling is disabled and its own retries are not used; this module runs the tool loop, the
retries and the schema repair itself.
"""

import json
import logging
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

import httpx
from google import genai
from google.genai import errors, types
from pydantic import BaseModel, ValidationError

from grocery_planner.env import getenv
from grocery_planner.llm.ratelimit import DEFAULT_RATE_LIMITER, RateLimiter
from grocery_planner.llm.types import (
    BudgetExhausted,
    GenerationHandle,
    GenerationStart,
    LLMError,
    LLMResult,
    LLMUnavailable,
    NoopGenerationHandle,
    OnRequest,
    QuotaExhausted,
    SchemaValidationFailed,
    ToolCallRecord,
    ToolLoopExceeded,
    ToolSpec,
    Usage,
    execute_tool,
)

log = logging.getLogger(__name__)
# The SDK warns whenever a reply holds a function call; we read the parts ourselves.
logging.getLogger("google_genai.types").setLevel(logging.ERROR)

DEFAULT_MODEL = "gemini-3.1-flash-lite"
DEFAULT_FALLBACK_MODEL = "gemini-3.5-flash-lite"
_FROM_ENV: Any = object()  # sentinel: read GEMINI_FALLBACK_MODEL
_DEFAULT_LIMITER: Any = object()  # sentinel: the shared limiter, for real (non-injected) clients


def resolve_models(
    model: str | None = None, fallback_model: str | None = _FROM_ENV
) -> tuple[str, str | None]:
    """(primary, fallback or None), from arguments, then the environment, then defaults."""
    primary = model or getenv("GEMINI_MODEL") or DEFAULT_MODEL
    if fallback_model is _FROM_ENV:
        fallback_model = getenv("GEMINI_FALLBACK_MODEL") or DEFAULT_FALLBACK_MODEL
        if fallback_model.strip().lower() in ("none", "off", "disabled"):
            fallback_model = None  # pinned runs, e.g. evals
    return primary, (None if fallback_model == primary else fallback_model)


MAX_ATTEMPTS = 3  # attempts per request, including the first (5xx, per-minute and unknown 429)
TIMEOUT_ATTEMPTS = 2  # timeouts and 504 DEADLINE_EXCEEDED: fail faster, one retry only
BACKOFF_SECONDS = (1.0, 2.0)  # before retry n
RETRY_DELAY_CAP = 20.0  # longest retryDelay hint we will wait out
TIMEOUT_MS = 20_000


def classify_429(err: errors.APIError) -> str:
    """per_day, per_minute or unknown, from the error's quota violation ids, then its text."""
    text = json.dumps(err.details, default=str).lower() + " " + str(err.message).lower()
    if "perday" in text or "per day" in text or "daily" in text:
        return "per_day"  # checked first: a daily limit is not cured by waiting a few seconds
    if "perminute" in text or "per minute" in text:
        return "per_minute"
    return "unknown"


def retry_delay_hint(err: errors.APIError) -> float | None:
    """Seconds Google asks us to wait: RetryInfo.retryDelay ("39s"), else "retry in 39.5s"."""
    details = err.details.get("error", err.details) if isinstance(err.details, dict) else {}
    for item in details.get("details", []) if isinstance(details, dict) else []:
        if isinstance(item, dict) and "retryDelay" in item:
            m = re.fullmatch(r"\s*([\d.]+)s\s*", str(item["retryDelay"]))
            if m:
                return float(m.group(1))
    m = re.search(r"retry in ([\d.]+)\s*s", str(err.message), re.IGNORECASE)
    return float(m.group(1).rstrip(".")) if m else None


@dataclass
class _Call:
    """Accumulates usage and tool calls for one generate / generate_with_tools call."""

    model: str
    fell_back: bool = False
    fallback_reason: str | None = None
    usage: Usage = field(default_factory=Usage)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    repair_used: bool = False


class GeminiClient:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        thinking_level: str | None = "minimal",
        on_request: OnRequest | None = None,
        genai_client: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
        clock: Callable[[], float] = time.perf_counter,
        single_shot: bool = False,
        fallback_model: str | None = _FROM_ENV,
        before_request: Callable[[str], None] | None = None,
        fallback_allowed: Callable[[], bool] | None = None,
        on_quota_exhausted: Callable[[str, str], None] | None = None,
        rate_limiter: RateLimiter | None = _DEFAULT_LIMITER,
        start_generation: Callable[[GenerationStart], GenerationHandle] | None = None,
        on_tool_call: Callable[[ToolCallRecord], None] | None = None,
    ) -> None:
        self.model, fallback = resolve_models(model, fallback_model)
        # None means no fallback. Never used by the single-shot probe.
        self.fallback_model = None if single_shot else fallback
        self._before_request = before_request
        self._fallback_allowed = fallback_allowed
        self._on_quota_exhausted = on_quota_exhausted
        self._start_generation = start_generation
        self._on_tool_call = on_tool_call
        if rate_limiter is _DEFAULT_LIMITER:  # only real clients share the process-wide limiter
            rate_limiter = DEFAULT_RATE_LIMITER if genai_client is None else None
        self._limiter = rate_limiter
        self.thinking_level = thinking_level  # None = leave the model's default
        self._on_request = on_request
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._clock = clock
        self._single_shot = single_shot  # one send, no retries, no repair (used by the probe)
        if genai_client is None:
            key = api_key or getenv("GEMINI_API_KEY")
            if not key:
                raise RuntimeError("GEMINI_API_KEY is not set (see .env.example)")
            genai_client = genai.Client(api_key=key)
        self._genai = genai_client

    # ---- public interface ---------------------------------------------------------------

    def generate[T: BaseModel](
        self, system: str, user: str, output_type: type[T], temperature: float = 0.1
    ) -> LLMResult[T]:
        def attempt(call: _Call, start: float) -> LLMResult[T]:
            config = self._config(system, temperature, output_type, tools=None)
            contents = [_user(user)]
            response = self._request(call, contents, config)
            return self._finish(call, start, contents, response, output_type, config)

        return self._with_fallback(attempt)

    def generate_with_tools[T: BaseModel](
        self,
        system: str,
        user: str,
        output_type: type[T],
        tools: list[ToolSpec],
        max_tool_rounds: int,
        temperature: float = 0.1,
    ) -> LLMResult[T]:
        def attempt(call: _Call, start: float) -> LLMResult[T]:
            by_name = {t.name: t for t in tools}
            plain = self._config(system, temperature, output_type, tools=None)
            config = self._config(system, temperature, output_type, tools=tools)
            # Once the allowed rounds are used up, the next request must be the answer: tool
            # calling is switched off for it rather than letting the model ask again.
            answer_now = config.model_copy(
                update={
                    "tool_config": types.ToolConfig(
                        function_calling_config=types.FunctionCallingConfig(
                            mode=types.FunctionCallingConfigMode.NONE
                        )
                    )
                }
            )
            contents = [_user(user)]
            rounds = 0
            while True:
                response = self._request(
                    call,
                    contents,
                    answer_now if rounds >= max_tool_rounds else config,
                    kind="request" if rounds == 0 else "tool_round",
                )
                function_calls = response.function_calls or []
                if not function_calls:
                    return self._finish(call, start, contents, response, output_type, plain)
                rounds += 1
                if rounds > max_tool_rounds:
                    raise ToolLoopExceeded(
                        f"model still asking for tools after {max_tool_rounds} rounds"
                    )
                # Echo the model's turn unchanged (Gemini 3 needs its thought signatures back).
                contents.append(response.candidates[0].content)
                parts = []
                for fc in function_calls:
                    result, record = execute_tool(
                        by_name, fc.name, dict(fc.args or {}), self._clock
                    )
                    call.tool_calls.append(record)
                    self._report_tool_call(record)
                    parts.append(types.Part.from_function_response(name=fc.name, response=result))
                contents.append(types.Content(role="user", parts=parts))

        return self._with_fallback(attempt)

    def _with_fallback[T: BaseModel](
        self, attempt: Callable[[_Call, float], LLMResult[T]]
    ) -> LLMResult[T]:
        """Run on the primary model. If it is unavailable (retries exhausted) or out of daily
        quota, run the whole call once more on the fallback model, from the start: a tool
        conversation cannot move between models (thought signatures are model-specific).
        Quotas are per model, so a per-day 429 on the primary says nothing about the fallback."""
        start, call = self._clock(), _Call(model=self.model)
        try:
            return attempt(call, start)
        except (LLMUnavailable, QuotaExhausted) as e:
            self._report_quota(call.model, e)
            if (
                self.fallback_model is None
                or (isinstance(e, QuotaExhausted) and e.kind != "per_day")
                or (self._fallback_allowed is not None and not self._fallback_allowed())
            ):
                raise
            reason = "unavailable" if isinstance(e, LLMUnavailable) else "per_day quota"
            call.model, call.fell_back, call.fallback_reason = self.fallback_model, True, reason
            call.tool_calls, call.repair_used = [], False  # the answering run starts clean
            try:
                return attempt(call, start)
            except QuotaExhausted as e2:
                self._report_quota(call.model, e2)
                raise

    def _report_tool_call(self, record: ToolCallRecord) -> None:
        if self._on_tool_call is not None:
            try:
                self._on_tool_call(record)
            except Exception:
                log.warning("tracing failed to record a tool call", exc_info=True)

    def _report_quota(self, model: str, exc: Exception) -> None:
        """Tell the budget layer when Google (not our counter) says a model is out for the day."""
        if (
            self._on_quota_exhausted
            and isinstance(exc, QuotaExhausted)
            and not isinstance(exc, BudgetExhausted)
            and exc.kind == "per_day"
        ):
            self._on_quota_exhausted(model, "per_day")

    # ---- internals ----------------------------------------------------------------------

    def _config(
        self,
        system: str,
        temperature: float,
        output_type: type[BaseModel],
        tools: list[ToolSpec] | None,
    ) -> types.GenerateContentConfig:
        return types.GenerateContentConfig(
            system_instruction=system,
            temperature=temperature,
            response_mime_type="application/json",
            response_json_schema=output_type.model_json_schema(),
            thinking_config=(
                types.ThinkingConfig(thinking_level=self.thinking_level.upper())
                if self.thinking_level
                else None
            ),
            tools=(
                [
                    types.Tool(
                        function_declarations=[
                            types.FunctionDeclaration(
                                name=t.name,
                                description=t.description,
                                parameters_json_schema=t.input_model.model_json_schema(),
                            )
                            for t in tools
                        ]
                    )
                ]
                if tools
                else None
            ),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            http_options=types.HttpOptions(timeout=TIMEOUT_MS),
        )

    def _emit(self, call: _Call, delta: Usage) -> None:
        delta = replace(delta, model=call.model)
        call.usage += delta
        if self._on_request:
            self._on_request(delta)

    def _request(
        self,
        call: _Call,
        contents: list,
        config: types.GenerateContentConfig,
        kind: str = "request",
    ):
        """One logical request, retried per the failure type. Every send is counted."""
        attempt = 0
        while True:
            attempt += 1
            if self._before_request:  # budget: may raise BudgetExhausted
                self._before_request(call.model)
            if self._limiter:  # per-minute: waits for a slot, or raises LLMUnavailable
                self._limiter.acquire(call.model)
            handle = self._begin_generation(call, contents, config, kind, attempt)
            try:
                response = self._genai.models.generate_content(
                    model=call.model, contents=contents, config=config
                )
            except errors.ClientError as e:
                self._emit(call, Usage(requests=1))
                _end_generation(handle, error=_error_label(e))
                if e.code != 429:
                    raise LLMError(f"{e.code} {e.status}: {e.message}") from e
                kind = classify_429(e)
                if kind == "per_day":
                    raise QuotaExhausted("per_day", str(e.message)) from e
                hint = retry_delay_hint(e)
                if hint is not None and hint > RETRY_DELAY_CAP:
                    raise QuotaExhausted("per_minute", f"retry delay {hint:g}s") from e
                delay = (
                    hint + self._rng.uniform(0, 0.5) if hint is not None else self._backoff(attempt)
                )
                if attempt >= self._limit(MAX_ATTEMPTS):
                    raise QuotaExhausted(kind, str(e.message)) from e
            except (errors.ServerError, httpx.TransportError, TimeoutError) as e:
                self._emit(call, Usage(requests=1))
                _end_generation(handle, error=_error_label(e))
                is_timeout = not isinstance(e, errors.ServerError) or e.code == 504
                limit = self._limit(TIMEOUT_ATTEMPTS if is_timeout else MAX_ATTEMPTS)
                if attempt >= limit:
                    raise LLMUnavailable(f"{type(e).__name__} after {attempt} attempts") from e
                delay = self._backoff(attempt)
            else:
                um = response.usage_metadata
                usage = Usage(
                    requests=1,
                    input_tokens=(um.prompt_token_count or 0) if um else 0,
                    output_tokens=(um.candidates_token_count or 0) if um else 0,
                    thinking_tokens=(um.thoughts_token_count or 0) if um else 0,
                    model=call.model,
                )
                self._emit(call, usage)
                _end_generation(handle, output=_describe_output(response), usage=usage)
                return response
            self._sleep(delay)

    def _begin_generation(self, call, contents, config, kind, attempt) -> GenerationHandle:
        """Tell the tracer a send is about to happen. Never raises: tracing must not break a run."""
        if self._start_generation is None:
            return NoopGenerationHandle()
        try:
            user, last_turn = _describe_contents(contents)
            return self._start_generation(
                GenerationStart(
                    model=call.model,
                    attempt=attempt,
                    kind=kind,
                    fell_back=call.fell_back,
                    system=str(config.system_instruction or ""),
                    user=user,
                    last_turn=last_turn,
                    turns=len(contents),
                    temperature=config.temperature,
                )
            )
        except Exception:
            log.warning("tracing failed to start a generation", exc_info=True)
            return NoopGenerationHandle()

    def _backoff(self, attempt: int) -> float:
        base = BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS)) - 1]
        return base + self._rng.uniform(0, 0.25 * base)

    def _limit(self, attempts: int) -> int:
        return 1 if self._single_shot else attempts

    def _finish[T: BaseModel](
        self,
        call: _Call,
        start: float,
        contents: list,
        response,
        output_type: type[T],
        config: types.GenerateContentConfig,
    ) -> LLMResult[T]:
        """Validate the final text with Pydantic; one repair request on failure."""
        text = _text(response)
        parsed, problems = _parse(output_type, text)
        if parsed is None and self._single_shot:
            raise SchemaValidationFailed([text], problems)
        if parsed is None:
            call.repair_used = True
            repair = [
                *contents,
                types.Content(role="model", parts=[types.Part(text=text or "(empty reply)")]),
                _user(
                    "Your previous reply failed validation:\n- "
                    + "\n- ".join(problems)
                    + "\nReply again with corrected JSON only."
                ),
            ]
            second = _text(self._request(call, repair, config, kind="repair"))
            parsed, problems2 = _parse(output_type, second)
            if parsed is None:
                raise SchemaValidationFailed([text, second], problems + problems2)
        return LLMResult(
            parsed=parsed,
            model=call.model,
            attempts=call.usage.requests,
            repair_used=call.repair_used,
            tool_calls=call.tool_calls,
            usage=call.usage,
            latency_ms=round((self._clock() - start) * 1000),
            fell_back=call.fell_back,
            fallback_reason=call.fallback_reason,
        )


def _user(text: str) -> types.Content:
    return types.Content(role="user", parts=[types.Part(text=text)])


def _text(response) -> str:
    candidates = response.candidates or []
    parts = candidates[0].content.parts if candidates and candidates[0].content else []
    return "".join(p.text for p in parts or [] if p.text and not p.thought)


def _parse[T: BaseModel](output_type: type[T], text: str) -> tuple[T | None, list[str]]:
    try:
        return output_type.model_validate_json(text), []
    except ValidationError as e:
        return None, [f"{'.'.join(map(str, x['loc'])) or '(root)'}: {x['msg']}" for x in e.errors()]


def _part_text(part: Any) -> str:
    if getattr(part, "function_call", None):
        fc = part.function_call
        return f"[function_call {fc.name} {json.dumps(dict(fc.args or {}), default=str)}]"
    if getattr(part, "function_response", None):
        fr = part.function_response
        return f"[function_response {fr.name} {json.dumps(fr.response, default=str)}]"
    return part.text or ""


def _describe_contents(contents: list) -> tuple[str, str | None]:
    """(the first user payload, the newest turn if it is a different one) for tracing."""
    texts = ["".join(_part_text(p) for p in (c.parts or [])) for c in contents]
    return texts[0], (texts[-1] if len(texts) > 1 else None)


def _describe_output(response: Any) -> Any:
    calls = response.function_calls or []
    if calls:
        return {"function_calls": [{"name": c.name, "args": dict(c.args or {})} for c in calls]}
    return {"text": _text(response)}


def _error_label(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    return f"{type(exc).__name__}({code})" if code else type(exc).__name__


def _end_generation(handle: GenerationHandle, **kw: Any) -> None:
    try:
        handle.end(**kw)
    except Exception:
        log.warning("tracing failed to end a generation", exc_info=True)
