"""A tracer that records the exact tree it is given. For tests (and for debugging by hand)."""

from dataclasses import dataclass, field
from typing import Any

from grocery_planner.llm.types import GenerationHandle, GenerationStart, ToolCallRecord, Usage
from grocery_planner.tracing.base import RunTrace, RunTraceInfo, StepTrace


@dataclass
class Node:
    kind: str  # trace | step | generation | tool
    name: str
    attrs: dict[str, Any] = field(default_factory=dict)
    children: list["Node"] = field(default_factory=list)
    ended: bool = False

    def shape(self) -> tuple:
        """(name, [children shapes]), ignoring attributes."""
        return (self.name, [c.shape() for c in self.children])

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()


class _Handle:
    def __init__(self, node: Node) -> None:
        self._node = node

    def end(self, *, output=None, usage: Usage | None = None, error=None) -> None:
        self._node.attrs.update(output=output, usage=usage, error=error)
        self._node.ended = True


class _Step:
    def __init__(self, node: Node) -> None:
        self.node = node

    def start_generation(self, info: GenerationStart) -> GenerationHandle:
        child = Node(
            "generation", "llm_request",
            dict(
                model=info.model, attempt=info.attempt, kind=info.kind, fell_back=info.fell_back,
                system=info.system, user=info.user, last_turn=info.last_turn,
            ),
        )  # fmt: skip
        self.node.children.append(child)
        return _Handle(child)

    def tool_call(self, record: ToolCallRecord) -> None:
        self.node.children.append(
            Node("tool", record.name, dict(args=record.args, summary=record.result_summary,
                                           error=record.error), ended=True)
        )  # fmt: skip

    def end(self, *, output=None, metadata=None, error=None) -> None:
        self.node.attrs.update(output=output, metadata=metadata or {}, error=error)
        self.node.ended = True


class _Run:
    def __init__(self, root: Node, trace_id: str) -> None:
        self.root, self.trace_id = root, trace_id

    def start_step(self, name, *, prompt_ref, attempt, input=None) -> StepTrace:
        node = Node("step", name, dict(prompt_ref=prompt_ref, attempt=attempt, input=input))
        self.root.children.append(node)
        return _Step(node)

    def finish(self, *, status, output=None, metadata=None) -> None:
        self.root.attrs.update(status=status, output=output, finish_metadata=metadata or {})
        self.root.ended = True


class RecordingTracer:
    enabled = True

    def __init__(self) -> None:
        self.runs: list[Node] = []

    def start_run(self, info: RunTraceInfo) -> RunTrace:
        root = Node(
            "trace", "plan_run",
            dict(run_id=info.run_id, tags=info.tags, app_env=info.app_env,
                 metadata=info.metadata, input=info.input),
        )  # fmt: skip
        self.runs.append(root)
        return _Run(root, trace_id=f"recorded-{info.run_id}")

    def trace_url(self, trace_id: str) -> str | None:
        return f"https://example.invalid/trace/{trace_id}"

    @property
    def root(self) -> Node:
        return self.runs[-1]

    def observations(self, run: Node | None = None) -> int:
        """Billable units for a run: the trace itself plus every span, generation and tool span."""
        return sum(1 for _ in (run or self.root).walk())
