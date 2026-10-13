from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class PlannerConfig(BaseModel):
    """User-editable parameters. Out-of-range values are rejected, not clamped."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    savings_threshold_dollars: Decimal = Field(default=Decimal(8), ge=3, le=25)
    savings_threshold_pct: Decimal = Field(
        default=Decimal("0.08"), ge=Decimal("0.03"), le=Decimal("0.20")
    )
    stop_not_found_pct: Decimal = Field(
        default=Decimal("0.25"), ge=Decimal("0.10"), le=Decimal("0.40")
    )
    stop_comparable_pct: Decimal = Field(
        default=Decimal("0.50"), ge=Decimal("0.40"), le=Decimal("0.80")
    )
    pack_size_tolerance: Decimal = Field(
        default=Decimal("0.15"), ge=Decimal("0.05"), le=Decimal("0.25")
    )
