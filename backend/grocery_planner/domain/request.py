from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from grocery_planner.config import GUARDRAILS

Text60 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]


class Meal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    meal_name: Text60
    mains: list[Text60] = Field(min_length=1, max_length=4)
    sides: list[Text60] = Field(default_factory=list, max_length=4)
    people: int = Field(ge=1, le=12)
    days: int = Field(ge=1, le=14)
    avoid: str | None = Field(default=None, max_length=100)

    @field_validator("avoid", mode="before")
    @classmethod
    def _blank_avoid_is_none(cls, v: object) -> object:
        if isinstance(v, str):
            v = v.strip()
            return v or None  # an empty optional field means "no avoid"
        return v


class PlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    meals: list[Meal] = Field(min_length=1, max_length=7)
    include_pantry_staples: bool = False

    @model_validator(mode="after")
    def _dish_cap(self) -> "PlanRequest":
        dishes = sum(len(m.mains) + len(m.sides) for m in self.meals)
        if dishes > GUARDRAILS.max_dishes:
            raise ValueError(
                f"A plan can include at most {GUARDRAILS.max_dishes} dishes in total (mains and "
                f"sides across all meals); you have {dishes}. Remove some dishes or plan fewer "
                "meals."
            )
        return self
