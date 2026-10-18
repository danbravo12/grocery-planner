"""The public shapes of the API, as pydantic models. They are the OpenAPI contract the frontend's
types are generated from, and (as response models) a second filter: a field not named here
cannot leave the server. Money and quantities are strings, as stored (no float ever)."""

from typing import Any, Literal

from pydantic import BaseModel

from grocery_planner.domain.request import PlanRequest

Store = str  # "coles" | "woolworths" (open: Aldi is one enum line away)
Status = Literal["queued", "running", "completed", "stopped", "failed"]
Verdict = Literal["no_meaningful_difference", "one_store", "split"]
Tier = Literal["exact", "equivalent", "substitute", "not_found"]
Confidence = Literal["high", "medium", "low"]


class ErrorBody(BaseModel):
    code: str
    message: str


class FieldError(BaseModel):
    field: str
    message: str


class ValidationErrorBody(ErrorBody):
    fields: list[FieldError]


class UnavailableBody(ErrorBody):
    reason: str | None = None
    reset_at_utc: str | None = None  # absent for provider_billing
    reset_at_sydney: str | None = None


class Health(BaseModel):
    status: str


class Availability(BaseModel):
    available: bool
    reason: str | None
    message: str
    reset_at_utc: str | None  # null when reason is provider_billing
    reset_at_sydney: str | None
    sample_run_id: str | None


class RunAccepted(BaseModel):
    run_id: str
    run_url: str
    events_url: str


class IngredientItem(BaseModel):
    item_id: str
    name: str
    quantity: str
    unit: str
    category: str
    meals: list[str]
    brand: str | None = None
    pantry_staple: bool
    in_vocabulary: bool
    flags: list[str] = []


class IngredientList(BaseModel):
    items: list[IngredientItem]
    assumptions: list[str]


class Pick(BaseModel):
    """One store's answer for one item. Everything but the tier is empty for not_found."""

    match_tier: Tier
    match_confidence: Confidence
    product_id: str | None = None
    product_name: str | None = None
    brand: str | None = None
    brand_label: Literal["house_brand", "branded"] | None = None
    pack_size: str | None = None
    pack_unit: str | None = None
    price: str | None = None
    regular_price: str | None = None
    unit_price: str | None = None
    on_special: bool = False
    special_end_date: str | None = None
    packs_needed: int | None = None
    purchased_qty: str | None = None
    leftover_qty: str | None = None
    total_cost: str | None = None
    regular_total_cost: str | None = None
    price_timestamp: str | None = None
    data_source: str | None = None
    reason: str | None = None


class ComparisonItem(BaseModel):
    item_id: str
    name: str
    quantity: str | None = None
    unit: str | None = None
    stores: dict[Store, Pick]


class Flag(BaseModel):
    kind: str
    item_id: str | None = None
    store: Store | None = None
    ends_on: str | None = None
    ends_in_days: int | None = None
    message: str


class Tip(BaseModel):
    item_id: str
    text: str


class Recheck(BaseModel):
    item_id: str
    hint: str


class Fallback(BaseModel):
    kind: Verdict
    store: Store | None = None
    assignment: dict[str, Store] | None = None
    totals: dict[Store, str]
    gap: str
    threshold: str
    split_savings: str


class Condition(BaseModel):
    valid_until: str  # ISO date
    items: list[str]
    fallback: Fallback


class RuleVerdict(BaseModel):
    kind: Verdict
    store: Store | None = None
    totals: dict[Store, str]
    gap: str
    threshold: str
    split_total: str | None = None  # the best split's basket total; absent on older runs
    split_savings: str
    condition: Condition | None = None


class Recommendation(BaseModel):
    recommendation: Verdict
    store: Store | None = None
    split_assignment: dict[str, Store] | None = None
    savings: str
    summary_line: str
    reasoning: str
    confidence: Confidence
    overridden: bool
    override_reason: str | None = None
    tips: list[Tip] = []
    recheck_items: list[Recheck] = []
    assumptions: list[str] = []
    flags: list[Flag] = []
    model: str | None = None
    fell_back: bool = False
    prompt_version: str | None = None
    rule_verdict: RuleVerdict


class Parameters(BaseModel):
    config: dict[str, str] = {}
    as_of: str | None = None
    stores: list[Store] = []
    prompt_versions: dict[str, str] = {}


class Totals(BaseModel):
    requests: int
    request_budget: int  # the worst case reserved for the run (what the budget guard holds)
    tokens_in: int
    tokens_out: int
    thinking_tokens: int
    latency_ms: int | None = None
    models: list[str]
    fell_back: bool
    loopback_used: bool
    would_cost_usd: str


class RunView(BaseModel):
    id: str
    status: Status
    stop_reason: str | None = None
    failure_reason: str | None = None
    message: str | None = None
    created_at: str
    completed_at: str | None = None
    request: PlanRequest
    parameters: Parameters
    ingredient_list: IngredientList | None = None
    comparison: list[ComparisonItem]
    recommendation: Recommendation | None = None
    totals: Totals


class ToolCall(BaseModel):
    name: str | None = None
    args: dict[str, Any] | None = None
    result: str | None = None
    latency_ms: float | None = None
    error: str | None = None


class Step(BaseModel):
    seq: int
    name: Literal["ingredient", "price", "planner", "reprice", "planner_2", "stop"]
    agent: str | None = None
    attempt: int
    status: Literal["running", "completed", "failed"]
    summary: str | None = None
    input: Any = None
    output: Any = None
    tool_calls: list[ToolCall]
    requests: int
    tokens_in: int
    tokens_out: int
    thinking_tokens: int
    latency_ms: int | None = None
    model: str | None = None
    fell_back: bool
    error: str | None = None
    started_at: str
    finished_at: str | None = None


class StepsView(BaseModel):
    run_id: str
    steps: list[Step]
