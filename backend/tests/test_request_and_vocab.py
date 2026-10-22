import pytest
from pydantic import ValidationError

from grocery_planner.config import ACTIVE_STORES
from grocery_planner.domain.request import Meal, PlanRequest
from grocery_planner.tools.price_lookup import SeededFilePriceSource
from grocery_planner.tools.vocabulary import ingredient_vocabulary

OK = {"meal_name": "Friday dinner", "mains": ["fillet mignon"], "people": 2, "days": 1}


def meal(**over) -> Meal:
    return Meal(**{**OK, **over})


def test_valid_meal_defaults():
    m = meal()
    assert m.sides == [] and m.avoid is None
    assert PlanRequest(meals=[m]).include_pantry_staples is False


def test_whitespace_is_stripped_and_empty_strings_rejected():
    m = meal(meal_name="  Friday  ", mains=["  steak "], sides=[" fries"], avoid="  nuts ")
    assert (m.meal_name, m.mains, m.sides, m.avoid) == ("Friday", ["steak"], ["fries"], "nuts")
    for bad in ({"meal_name": "   "}, {"mains": [" "]}, {"sides": [""]}, {"mains": []}):
        with pytest.raises(ValidationError):
            meal(**bad)


def test_blank_avoid_means_no_avoid():
    assert meal(avoid="   ").avoid is None and meal(avoid="").avoid is None


@pytest.mark.parametrize(
    "over",
    [
        {"meal_name": "x" * 61},
        {"mains": ["a"] * 5},
        {"mains": ["x" * 61]},
        {"sides": ["a"] * 5},
        {"people": 0},
        {"people": 13},
        {"days": 0},
        {"days": 15},
        {"avoid": "x" * 101},
    ],
)
def test_caps_are_enforced(over):
    with pytest.raises(ValidationError):
        meal(**over)


def test_caps_have_inclusive_edges():
    m = meal(
        meal_name="x" * 60, mains=["a"] * 4, sides=["b"] * 4, people=12, days=14, avoid="y" * 100
    )
    assert m.people == 12 and m.days == 14


def test_plan_request_meal_count():
    assert len(PlanRequest(meals=[meal()] * 7).meals) == 7
    for n in (0, 8):
        with pytest.raises(ValidationError):
            PlanRequest(meals=[meal()] * n)


def test_vocabulary_shape():
    vocab = ingredient_vocabulary()
    assert len(vocab) == 168
    assert vocab["beef eye fillet"].category == "meat" and vocab["beef eye fillet"].unit == "g"
    assert vocab["eggs"].unit == "each" and vocab["milk"].unit == "ml"
    staples = {e.name for e in vocab.values() if e.pantry_staple}
    assert {"salt", "pepper", "olive oil", "vegetable oil"} <= staples
    assert "butter" not in staples
    assert list(vocab) == [
        e.name for e in sorted(vocab.values(), key=lambda e: (e.category, e.name))
    ]


def test_every_vocabulary_name_is_searchable_in_the_catalogue():
    source = SeededFilePriceSource()
    for entry in ingredient_vocabulary().values():
        found = [p for s in ACTIVE_STORES for p in source.search(entry.name, s, 50)]
        assert found, entry.name
        assert all(p.pack_unit == entry.unit for p in found if p.search_terms[0] == entry.name)


def test_total_dishes_are_capped_at_twenty():
    def meals(n_dishes):
        out, left = [], n_dishes
        while left > 0:
            mains = min(4, left)
            sides = min(4, left - mains)
            out.append(meal(meal_name=f"m{len(out)}", mains=["x"] * mains, sides=["y"] * sides))
            left -= mains + sides
        return out

    assert sum(len(m.mains) + len(m.sides) for m in PlanRequest(meals=meals(20)).meals) == 20
    with pytest.raises(ValidationError) as e:
        PlanRequest(meals=meals(21))
    message = str(e.value)
    assert "at most 20 dishes in total" in message and "you have 21" in message
    # the per-field caps still apply on their own
    with pytest.raises(ValidationError):
        meal(mains=["a"] * 5)
