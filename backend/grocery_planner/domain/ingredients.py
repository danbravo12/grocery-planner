from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, WithJsonSchema

from grocery_planner.domain.catalogue import PackUnit
from grocery_planner.llm.types import Usage

# Decimal in code; plain "number" in the schema sent to the model.
Quantity = Annotated[Decimal, WithJsonSchema({"type": "number"})]


class DishIngredient(BaseModel):
    """One ingredient of one dish, per single serving, as the model returns it."""

    name: str
    quantity_per_serving: Quantity = Field(gt=0)
    unit: Literal["g", "ml", "each"]
    in_vocabulary: bool
    notes: str = ""
    brand: str = ""  # only when the dish text names a brand; "" means no brand


class DishResult(BaseModel):
    """A dish expanded to per-serving ingredients. This is what dish_cache stores."""

    ingredients: list[DishIngredient]
    assumptions: list[str] = Field(default_factory=list)


class ModelDish(DishResult):
    dish_id: str


class ExpansionBatch(BaseModel):
    """The model's whole output: one entry per requested dish."""

    dishes: list[ModelDish]


class IngredientItem(BaseModel):
    item_id: str = ""  # i1, i2, ... assigned by the agent after sorting; stable within a run
    name: str
    quantity: Decimal
    unit: PackUnit
    category: str
    meals: list[str]
    brand: str | None = None  # named by the user in the dish text, otherwise None
    pantry_staple: bool
    in_vocabulary: bool
    flags: list[str] = Field(default_factory=list)


class CacheStats(BaseModel):
    hits: int
    misses: int


class IngredientList(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    items: list[IngredientItem]
    assumptions: list[str]
    usage: Usage
    cache: CacheStats
