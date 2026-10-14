from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from grocery_planner.domain.catalogue import BrandLabel, PackUnit, Store

MatchTier = Literal["exact", "equivalent", "substitute", "not_found"]
MatchConfidence = Literal["high", "medium", "low"]

# special_end_date is legitimately null for matched rows that are not on special.
_PRODUCT_FIELDS = (
    "product_id",
    "product_name",
    "brand",
    "brand_label",
    "pack_size",
    "pack_unit",
    "price",
    "unit_price",
    "packs_needed",
    "purchased_qty",
    "leftover_qty",
    "total_cost",
    "price_timestamp",
    "data_source",
)


class PricedItem(BaseModel):
    """The price agent's output for one item at one store."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    item_id: str
    item_name: str
    category: str
    store: Store
    product_id: str | None = None
    product_name: str | None = None
    brand: str | None = None
    brand_label: BrandLabel | None = None
    pack_size: Decimal | None = None
    pack_unit: PackUnit | None = None
    price: Decimal | None = None
    unit_price: Decimal | None = None
    on_special: bool = False
    special_end_date: date | None = None
    packs_needed: int | None = None
    purchased_qty: Decimal | None = None  # packs_needed x pack_size
    leftover_qty: Decimal | None = None  # purchased_qty - needed; shown, never credited
    total_cost: Decimal | None = None
    match_tier: MatchTier
    match_confidence: MatchConfidence
    price_timestamp: datetime | None = None
    data_source: Literal["seeded"] | None = None

    @field_validator(
        "pack_size",
        "price",
        "unit_price",
        "total_cost",
        "purchased_qty",
        "leftover_qty",
        mode="before",
    )
    @classmethod
    def _no_floats(cls, v: object) -> object:
        if isinstance(v, float):
            raise ValueError("money and sizes must be Decimal, str or int, never float")
        return v

    @model_validator(mode="after")
    def _product_fields_match_tier(self) -> Self:
        missing = [f for f in _PRODUCT_FIELDS if getattr(self, f) is None]
        if self.match_tier == "not_found":
            if len(missing) != len(_PRODUCT_FIELDS) or self.on_special or self.special_end_date:
                raise ValueError("not_found rows must have no product fields")
        elif missing:
            raise ValueError(f"{self.match_tier} rows need {', '.join(missing)}")
        return self
