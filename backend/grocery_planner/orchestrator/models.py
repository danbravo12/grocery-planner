from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field

RunStatus = Literal["queued", "running", "completed", "stopped", "failed"]
TERMINAL = ("completed", "stopped", "failed")
StepName = Literal["ingredient", "price", "planner", "reprice", "planner_2", "stop"]


class StepRecord(BaseModel):
    seq: int
    name: StepName
    agent: Literal["ingredient", "price", "planner"] | None = None
    attempt: int = 1  # 2 for the loop-back pass (reprice, planner_2)
    status: Literal["running", "completed", "failed"] = "running"
    input: Any = None
    output: Any = None
    tool_calls: list[Any] = Field(default_factory=list)
    requests: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    thinking_tokens: int = 0
    latency_ms: int | None = None
    model: str | None = None
    fell_back: bool = False
    summary: str | None = None
    error: str | None = None
    started_at: datetime
    finished_at: datetime | None = None


class RunRecord(BaseModel):
    id: str
    status: RunStatus = "queued"
    stop_reason: str | None = None
    failure_reason: str | None = None
    user_message: str | None = None
    input: Any = None
    parameters: Any = None
    ingredient_output: Any = None
    price_output: Any = None
    planner_output: Any = None
    models_used: list[str] = Field(default_factory=list)
    fell_back: bool = False
    requests: int = 0
    estimated_requests: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    thinking_tokens: int = 0
    cost_usd: Decimal = Decimal(0)  # what was actually charged (the free tier charges nothing)
    would_cost_usd: Decimal = Decimal(0)  # what the run WOULD cost on Google's paid tier
    latency_ms: int | None = None
    loopback_used: bool = False
    langfuse_trace_id: str | None = None
    is_owner: bool = False  # started from the owner's browser: left out of the usage stats
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None


class RunEvent(BaseModel):
    """What SSE streams. `seq` counts from 1. Events are derived from stored state and only ever
    appended, so a client reconnecting with Last-Event-ID resumes exactly."""

    seq: int
    type: Literal["step_started", "step_completed", "loopback", "run_completed", "run_failed"]
    run_id: str
    step: int | None = None
    name: str | None = None
    attempt: int | None = None
    summary: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
