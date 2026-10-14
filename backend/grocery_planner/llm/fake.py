"""Scripted LLMClient for agent unit tests. No network, same interface as GeminiClient."""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from grocery_planner.llm.types import (
    GenerationHandle,
    GenerationStart,
    LLMResult,
    NoopGenerationHandle,
    OnRequest,
    ToolCallRecord,
    ToolLoopExceeded,
    ToolSpec,
    Usage,
    execute_tool,
)


@dataclass(frozen=True)
class FakeToolCalls:
    """The 'model' asks for these tools, one round per list entry, then returns `result`.
    Each round is a list of (tool_name, args)."""

    rounds: list[list[tuple[str, dict[str, Any]]]]
    result: BaseModel


@dataclass
class FakeLLMClient:
    """`script` has one entry per generate / generate_with_tools call:
    - a BaseModel instance: returned as the parsed result
    - an Exception instance: raised (e.g. QuotaExhausted, SchemaValidationFailed)
    - FakeToolCalls: tools are executed for real, then `result` is returned
    Every call consumes one entry and counts as one request, plus one per tool round."""

    script: list[Any]
    on_request: OnRequest | None = None
    before_request: Callable[[str], None] | None = None  # may raise, like the budget check
    start_generation: Callable[[GenerationStart], GenerationHandle] | None = None  # tracing
    on_tool_call: Callable[[ToolCallRecord], None] | None = None  # tracing
    model: str = "fake-model"
    calls: list[dict[str, Any]] = field(default_factory=list)  # what the agent sent

    def _next(self, system, user, output_type, tools=None):
        self.calls.append(
            {"system": system, "user": user, "output_type": output_type, "tools": tools}
        )
        self._last = (system, user)
        if not self.script:
            raise AssertionError("FakeLLMClient script exhausted")
        return self.script.pop(0)

    def _request(self, kind: str = "request") -> Usage:
        if self.before_request:
            self.before_request(self.model)
        handle: GenerationHandle = NoopGenerationHandle()
        if self.start_generation:
            system, user = self._last
            handle = self.start_generation(
                GenerationStart(self.model, 1, kind, False, system, user, None, 1, 0.1)
            )
        delta = Usage(requests=1, model=self.model)
        if self.on_request:
            self.on_request(delta)
        handle.end(output={"text": "(scripted)"}, usage=delta)
        return delta

    def generate[T: BaseModel](
        self, system: str, user: str, output_type: type[T], temperature: float = 0.1
    ) -> LLMResult[T]:
        step = self._next(system, user, output_type)
        if isinstance(step, Exception):
            raise step
        usage = self._request()
        return LLMResult(parsed=step, model=self.model, attempts=1, repair_used=False, usage=usage)

    def generate_with_tools[T: BaseModel](
        self,
        system: str,
        user: str,
        output_type: type[T],
        tools: list[ToolSpec],
        max_tool_rounds: int,
    ) -> LLMResult[T]:
        step = self._next(system, user, output_type, tools)
        if isinstance(step, Exception):
            raise step
        if not isinstance(step, FakeToolCalls):
            usage = self._request()
            return LLMResult(
                parsed=step, model=self.model, attempts=1, repair_used=False, usage=usage
            )
        by_name = {t.name: t for t in tools}
        usage, records = Usage(), []
        for n, round_calls in enumerate(step.rounds, start=1):
            # the request in which the model asked for these tools
            usage += self._request("request" if n == 1 else "tool_round")
            if n > max_tool_rounds:
                raise ToolLoopExceeded(
                    f"model still asking for tools after {max_tool_rounds} rounds"
                )
            for name, args in round_calls:
                _, record = execute_tool(by_name, name, args, lambda: 0.0)
                records.append(record)
                if self.on_tool_call:
                    self.on_tool_call(record)
        usage += self._request("tool_round")  # the final answer
        return LLMResult(
            parsed=step.result,
            model=self.model,
            attempts=usage.requests,
            repair_used=False,
            tool_calls=records,
            usage=usage,
        )
