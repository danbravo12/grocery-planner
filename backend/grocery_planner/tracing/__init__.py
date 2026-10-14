from grocery_planner.tracing.base import (
    NoopTracer,
    RunTraceInfo,
    SafeTracer,
    Tracer,
    current_app_env,
)
from grocery_planner.tracing.pricing import PAID_TIER_PRICES, PRICES_CHECKED_ON, would_cost
from grocery_planner.tracing.recording import RecordingTracer


def tracer_from_env() -> Tracer:
    """Langfuse if the keys are set, otherwise the no-op tracer."""
    from grocery_planner.tracing.langfuse_tracer import LangfuseTracer

    return LangfuseTracer.from_env() or NoopTracer()


__all__ = [
    "PAID_TIER_PRICES",
    "PRICES_CHECKED_ON",
    "NoopTracer",
    "RecordingTracer",
    "RunTraceInfo",
    "SafeTracer",
    "Tracer",
    "current_app_env",
    "tracer_from_env",
    "would_cost",
]
