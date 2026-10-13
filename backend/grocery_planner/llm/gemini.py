"""LLMClient on Google's google-genai SDK (generate_content API).

Hand-rolled on purpose: every request is visible and counted. The SDK's automatic function
calling is disabled and its own retries are not used; this module runs the tool loop, the
retries and the schema repair itself.
"""

import json
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
from google import genai
from google.genai import errors, types
from pydantic import BaseModel, ValidationError

from grocery_planner.env import getenv
from grocery_planner.llm.types import (
    LLMError,
    LLMResult,
    LLMUnavailable,
    OnRequest,
    QuotaExhausted,
    SchemaValidationFailed,
    ToolCallRecord,
    ToolLoopExceeded,
    ToolSpec,
    Usage,
    execute_tool,
)

DEFAULT_MODEL = "gemini-3.5-flash-lite"
MAX_ATTEMPTS = 3  # attempts per request, including the first
BACKOFF_SECONDS = (1.0, 2.0, 4.0)  # before retry n; only the first MAX_ATTEMPTS - 1 are used
TIMEOUT_MS = 30_000


def classify_429(err: errors.APIError) -> str:
    """per_day, per_minute or unknown, from the error's quota violation ids, then its text."""
    text = json.dumps(err.details, default=str).lower() + " " + str(err.message).lower()
    if "perday" in text or "per day" in text or "daily" in text:
        return "per_day"  # checked first: a daily limit is not cured by waiting a few seconds
    if "perminute" in text or "per minute" in text:
        return "per_minute"
    return "unknown"


@dataclass
class _Call:
    """Accumulates usage and tool calls for one generate / generate_with_tools call."""

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
    ) -> None:
        self.model = model or getenv("GEMINI_MODEL") or DEFAULT_MODEL
        self.thinking_level = thinking_level  # None = leave the model's default
        self._on_request = on_request
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._clock = clock
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
        start, call = self._clock(), _Call()
        config = self._config(system, temperature, output_type, tools=None)
        contents = [_user(user)]
        response = self._request(call, contents, config)
        return self._finish(call, start, contents, response, output_type, config)

    def generate_with_tools[T: BaseModel](
        self,
        system: str,
        user: str,
        output_type: type[T],
        tools: list[ToolSpec],
        max_tool_rounds: int,
        temperature: float = 0.1,
    ) -> LLMResult[T]:
        start, call = self._clock(), _Call()
        by_name = {t.name: t for t in tools}
        plain = self._config(system, temperature, output_type, tools=None)
        config = self._config(system, temperature, output_type, tools=tools)
        contents = [_user(user)]
        rounds = 0
        while True:
            response = self._request(call, contents, config)
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
                result, record = execute_tool(by_name, fc.name, dict(fc.args or {}), self._clock)
                call.tool_calls.append(record)
                parts.append(types.Part.from_function_response(name=fc.name, response=result))
            contents.append(types.Content(role="user", parts=parts))

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
        call.usage += delta
        if self._on_request:
            self._on_request(delta)

    def _request(self, call: _Call, contents: list, config: types.GenerateContentConfig):
        """One logical request: up to MAX_ATTEMPTS sends. Every send is counted."""
        failure: Exception | None = None
        kind = "unknown"
        for attempt in range(MAX_ATTEMPTS):
            try:
                response = self._genai.models.generate_content(
                    model=self.model, contents=contents, config=config
                )
            except errors.ClientError as e:
                self._emit(call, Usage(requests=1))
                if e.code != 429:
                    raise LLMError(f"{e.code} {e.status}: {e.message}") from e
                kind = classify_429(e)
                if kind == "per_day":
                    raise QuotaExhausted("per_day", str(e.message)) from e
                failure = e
            except (errors.ServerError, httpx.TransportError, TimeoutError) as e:
                self._emit(call, Usage(requests=1))
                kind, failure = "", e
            else:
                um = response.usage_metadata
                self._emit(
                    call,
                    Usage(
                        requests=1,
                        input_tokens=(um.prompt_token_count or 0) if um else 0,
                        output_tokens=(um.candidates_token_count or 0) if um else 0,
                        thinking_tokens=(um.thoughts_token_count or 0) if um else 0,
                    ),
                )
                return response
            if attempt < MAX_ATTEMPTS - 1:
                base = BACKOFF_SECONDS[attempt]
                self._sleep(base + self._rng.uniform(0, 0.25 * base))
        if kind:  # a 429 that outlasted every retry
            raise QuotaExhausted(kind, str(getattr(failure, "message", failure))) from failure
        raise LLMUnavailable(f"{type(failure).__name__} after {MAX_ATTEMPTS} attempts") from failure

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
            second = _text(self._request(call, repair, config))
            parsed, problems2 = _parse(output_type, second)
            if parsed is None:
                raise SchemaValidationFailed([text, second], problems + problems2)
        return LLMResult(
            parsed=parsed,
            model=self.model,
            attempts=call.usage.requests,
            repair_used=call.repair_used,
            tool_calls=call.tool_calls,
            usage=call.usage,
            latency_ms=round((self._clock() - start) * 1000),
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
