"""The event stream, derived from stored state so it can be replayed after a reconnect.

Nothing is held in memory by the running pipeline: `events_from_state` rebuilds the full,
append-only event list from the run row and its step rows. A client that saw events up to seq N
reconnects with N and gets exactly the rest.
"""

import time
from collections.abc import Callable, Iterator

from grocery_planner.orchestrator.models import TERMINAL, RunEvent, RunRecord, StepRecord
from grocery_planner.orchestrator.store import RunStore


def events_from_state(
    run: RunRecord, steps: list[StepRecord], after_seq: int = 0
) -> list[RunEvent]:
    events: list[RunEvent] = []

    def add(type_: str, **kw) -> None:
        events.append(RunEvent(seq=len(events) + 1, type=type_, run_id=run.id, **kw))

    for step in steps:
        if step.name == "reprice":  # the loop-back begins when the re-pricing step does
            first_planner = next((s for s in steps if s.name == "planner"), None)
            rechecks = [
                r["item_id"] for r in ((first_planner.output or {}).get("recheck_items") or [])
            ] if first_planner else []  # fmt: skip
            add(
                "loopback",
                summary=f"Re-checking {len(rechecks)} doubtful match(es) with the price agent",
                data={"item_ids": rechecks},
            )
        ident = dict(step=step.seq, name=step.name, attempt=step.attempt)
        add("step_started", **ident)
        if step.status == "completed":
            add(
                "step_completed",
                summary=step.summary,
                data={
                    "model": step.model,
                    "fell_back": step.fell_back,
                    "requests": step.requests,
                    "tokens_in": step.tokens_in,
                    "tokens_out": step.tokens_out,
                    "latency_ms": step.latency_ms,
                },  # fmt: skip
                **ident,
            )
    if run.status == "completed":
        add(
            "run_completed",
            summary=(run.planner_output or {}).get("summary_line"),
            data={"models": run.models_used},
        )
    elif run.status in ("stopped", "failed"):
        add(
            "run_failed",  # also used for a stop; data.status says which
            summary=run.user_message,
            data={
                "status": run.status,
                "reason": run.failure_reason or run.stop_reason,
                "models": run.models_used,
            },
        )
    return [e for e in events if e.seq > after_seq]


def follow_events(
    store: RunStore,
    run_id: str,
    after_seq: int = 0,
    poll_seconds: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
    max_wait_seconds: float | None = None,
) -> Iterator[RunEvent]:
    """Yield events with seq > `after_seq` as they appear, ending after the run's terminal
    event. Reads only stored state, so it behaves the same for a live run and a replay."""
    waited = 0.0
    while True:
        run, steps = store.get_run(run_id), store.get_steps(run_id)
        for event in events_from_state(run, steps, after_seq):
            after_seq = event.seq
            yield event
        if run.status in TERMINAL:
            return
        if max_wait_seconds is not None and waited >= max_wait_seconds:
            return
        sleep(poll_seconds)
        waited += poll_seconds
