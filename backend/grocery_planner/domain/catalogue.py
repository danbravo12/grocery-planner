from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator


class Store(StrEnum):
    # Adding a store is one line here plus seed rows; the DB has no CHECK on store.
    COLES = "coles"
    WOOLWORTHS = "woolworths"
    ALDI = "aldi"  # no seed rows yet; present so N-store logic can be exercised


BrandLabel = Literal["house_brand", "branded"]
PackUnit = Literal["g", "ml", "each"]
QualityBand = Literal["budget", "standard", "premium"]

_UNIT_PRICE_QUANT = Decimal("0.0001")


class CatalogueProduct(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    store: Store
    product_id: str
    product_name: str
    brand: str
    brand_label: BrandLabel
    category: str
    search_terms: list[str]
    pack_size: Decimal = Field(gt=0)
    pack_unit: PackUnit
    price: Decimal = Field(gt=0)  # current shelf price (the special price when on_special)
    on_special: bool = False
    special_end_date: date | None = None
    price_timestamp: datetime
    data_source: Literal["seeded"] = "seeded"
    quality_band: QualityBand

    @field_validator("price", "pack_size", mode="before")
    @classmethod
    def _no_floats(cls, v: object) -> object:
        if isinstance(v, float):
            raise ValueError("money and sizes must be Decimal, str or int, never float")
        return v

    @model_validator(mode="after")
    def _special_end_needs_special(self) -> Self:
        if self.special_end_date is not None and not self.on_special:
            raise ValueError("special_end_date set but on_special is false")
        return self

    @computed_field
    @property
    def unit_price(self) -> Decimal:
        """Price per 100g / 100ml, or per single item for 'each'."""
        per = Decimal(1) if self.pack_unit == "each" else Decimal(100)
        return (self.price / self.pack_size * per).quantize(_UNIT_PRICE_QUANT, ROUND_HALF_UP)
