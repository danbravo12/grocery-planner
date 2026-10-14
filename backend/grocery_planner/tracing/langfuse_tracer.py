"""Langfuse tracing (SDK v4, OpenTelemetry based).

Shape, about 10 billable units per run (the free plan has 50,000 a month):
    plan_run (trace + root span)
      ingredient | price | planner | reprice | planner_2 | stop      one span per orchestrator step
        llm_request                      one generation per model REQUEST: retries, repairs, the
                                         fallback model and tool rounds each get their own
        what_if_split                    a child of the planner span, per tool call
Code-only work (catalogue searches, packing, planner maths) gets no spans: a summary goes in the
step span's metadata instead.
"""

import logging
import threading
from contextlib import ExitStack
from typing import Any

from langfuse import Langfuse, propagate_attributes

from grocery_planner.env import getenv
from grocery_planner.llm.types import GenerationHandle, GenerationStart, ToolCallRecord, Usage
from grocery_planner.tracing.base import (
    RunTrace,
    RunTraceInfo,
    StepTrace,
    current_app_env,
    tracing_disabled_by_env,
)
from grocery_planner.tracing.pricing import PRICES_CHECKED_ON, would_cost

log = logging.getLogger(__name__)
COST_NOTE = (
    "cost is what this request WOULD cost on Google's paid tier "
    f"(prices checked {PRICES_CHECKED_ON}); the free tier actually charges $0"
)


class _Generation:
    def __init__(self, span: Any, info: GenerationStart) -> None:
        self._span, self._info = span, info

    def end(self, *, output: Any = None, usage: Usage | None = None, error: str | None = None):
        kw: dict[str, Any] = {"output": output}
        meta: dict[str, Any] = {"error": error} if error else {}
        if usage is not None:
            kw["usage_details"] = {
                "input": usage.input_tokens,
                "output": usage.output_tokens,
                "thinking": usage.thinking_tokens,
            }
            cost = would_cost(self._info.model, usage)
            if cost is not None:
                kw["cost_details"] = {
                    "input": float(cost.input),
                    "output": float(cost.output),
                    "total": float(cost.total),
                }
                meta["cost_note"] = COST_NOTE
        if error:
            kw["level"], kw["status_message"] = "ERROR", error
        self._span.update(metadata=meta or None, **kw)
        self._span.end()


class _Step:
    def __init__(self, span: Any, prompt_ref: str | None) -> None:
        self._span, self._prompt_ref = span, prompt_ref

    def start_generation(self, info: GenerationStart) -> GenerationHandle:
        payload: dict[str, Any] = {
            "system_prompt": self._prompt_ref or "(unversioned)",  # the name, not the full text
            "user": info.user,
        }
        if info.last_turn:
            payload["last_turn"] = info.last_turn
        span = self._span.start_observation(
            name="llm_request",
            as_type="generation",
            model=info.model,
            input=payload,
            model_parameters={"temperature": info.temperature}
            if info.temperature is not None
            else None,
            metadata={
                "attempt": info.attempt,
                "kind": info.kind,
                "fell_back": info.fell_back,
                "turns": info.turns,
            },
        )
        return _Generation(span, info)

    def tool_call(self, record: ToolCallRecord) -> None:
        # The tool already ran; this span records it (its duration is in metadata).
        span = self._span.start_observation(
            name=record.name,
            as_type="tool",
            input=record.args,
            output=record.result_summary,
            metadata={"latency_ms": record.latency_ms},
            level="ERROR" if record.error else None,
            status_message=record.error,
        )
        span.end()

    def end(self, *, output=None, metadata=None, error=None) -> None:
        kw: dict[str, Any] = {"output": output}
        if metadata:
            kw["metadata"] = metadata
        if error:
            kw["level"], kw["status_message"] = "ERROR", error
        self._span.update(**kw)
        self._span.end()


class _Run:
    def __init__(self, tracer: "LangfuseTracer", root: Any, stack: ExitStack, trace_id: str):
        self._tracer, self._root, self._stack, self.trace_id = tracer, root, stack, trace_id

    def start_step(self, name, *, prompt_ref, attempt, input=None) -> StepTrace:
        span = self._root.start_observation(
            name=name,
            as_type="span",
            input=input,
            metadata={"attempt": attempt, "prompt": prompt_ref},
        )
        return _Step(span, prompt_ref)

    def finish(self, *, status, output=None, metadata=None) -> None:
        try:
            self._root.update(output=output, metadata={"status": status, **(metadata or {})})
        finally:
            self._stack.close()  # ends the root span and leaves the trace context
            self._tracer.flush()


class LangfuseTracer:
    enabled = True

    def __init__(self, client: Langfuse, flush_timeout: float = 5.0) -> None:
        self._client, self._flush_timeout = client, flush_timeout

    @classmethod
    def from_env(cls) -> "LangfuseTracer | None":
        """A tracer if LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are set, otherwise None."""
        public, secret = getenv("LANGFUSE_PUBLIC_KEY"), getenv("LANGFUSE_SECRET_KEY")
        if tracing_disabled_by_env() or not public or not secret:
            return None
        host = getenv("LANGFUSE_HOST") or getenv("LANGFUSE_BASE_URL")
        client = Langfuse(
            public_key=public,
            secret_key=secret,
            base_url=host or None,
            environment=current_app_env(),
        )
        return cls(client)

    def start_run(self, info: RunTraceInfo) -> RunTrace:
        trace_id = self._client.create_trace_id(seed=info.run_id)  # the same run, the same trace
        stack = ExitStack()
        try:
            root = stack.enter_context(
                self._client.start_as_current_observation(
                    name="plan_run",
                    trace_context={"trace_id": trace_id},
                    input=info.input,
                    metadata=info.metadata,
                )
            )
            stack.enter_context(
                propagate_attributes(
                    trace_name="plan_run", tags=info.tags, environment=info.app_env
                )
            )
        except Exception:
            stack.close()
            raise
        return _Run(self, root, stack, trace_id)

    def trace_url(self, trace_id: str) -> str | None:
        return self._client.get_trace_url(trace_id=trace_id)

    def flush(self) -> None:
        """Send what is buffered, waiting a short while at most (never longer than the timeout)."""
        worker = threading.Thread(target=self._client.flush, name="langfuse-flush", daemon=True)
        worker.start()
        worker.join(self._flush_timeout)
        if worker.is_alive():
            log.warning("langfuse flush still running after %.0fs; moving on", self._flush_timeout)
