"""Live runs of the ingredient agent against the real Gemini API. Run with:
    uv run pytest -m live -s tests/test_ingredient_live.py
Uses 3 free-tier requests, plus 0 for the cache-hit re-run."""

import time

import pytest

from grocery_planner.agents.ingredient import run_ingredient_agent
from grocery_planner.db.dish_cache import InMemoryDishCache
from grocery_planner.domain.request import Meal, PlanRequest
from grocery_planner.env import getenv
from grocery_planner.llm import GeminiClient

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def client():
    if not getenv("GEMINI_API_KEY"):
        pytest.skip("GEMINI_API_KEY not set")
    return GeminiClient()


@pytest.fixture(scope="module")
def cache():
    return InMemoryDishCache()


def show(label, result, seconds):
    u = result.usage
    print(f"\n=== {label} ===")
    print(
        f"requests={u.requests} input_tokens={u.input_tokens} output_tokens={u.output_tokens} "
        f"thinking_tokens={u.thinking_tokens} latency={seconds:.1f}s "
        f"cache hits={result.cache.hits} misses={result.cache.misses}"
    )
    for i in result.items:
        flags = f"  FLAGS: {i.flags}" if i.flags else ""
        vocab = "" if i.in_vocabulary else "  [not in vocabulary]"
        row = f"  {i.name:<22} {i.quantity:>8} {i.unit:<4} {i.category:<8}"
        print(f"{row} meals={i.meals}{vocab}{flags}")
    for a in result.assumptions:
        print(f"  - {a}")


def go(client, cache, label, meals, staples=False):
    start = time.perf_counter()
    result = run_ingredient_agent(
        PlanRequest(meals=meals, include_pantry_staples=staples), client, cache
    )
    show(label, result, time.perf_counter() - start)
    return result


FRIDAY = dict(
    meal_name="Friday dinner",
    mains=["fillet mignon"],
    sides=["potato salad", "fries"],
    people=2,
    days=1,
)


def test_a_friday_dinner(client, cache):
    r = go(client, cache, "(a) Friday dinner", [Meal(**FRIDAY)])
    assert r.usage.requests == 1 and r.cache.misses == 3 and r.items


def test_b_two_meals_sharing_onion_and_garlic(client, cache):
    meals = [
        Meal(meal_name="Monday bolognese", mains=["spaghetti bolognese"], people=4, days=3),
        Meal(meal_name="Tuesday chilli", mains=["chilli con carne"], people=4, days=3),
    ]
    r = go(client, cache, "(b) shared onion and garlic", meals)
    shared = [i for i in r.items if len(i.meals) == 2]
    print("  merged across meals:", [(i.name, str(i.quantity), i.unit) for i in shared])
    assert r.usage.requests == 1 and shared


def test_c_creamy_potato_salad_avoiding_dairy(client, cache):
    meal = Meal(meal_name="BBQ", mains=["creamy potato salad"], people=3, days=2, avoid="dairy")
    r = go(client, cache, "(c) creamy potato salad, avoid dairy", [meal])
    dairy = {
        "milk",
        "cream",
        "butter",
        "cheddar",
        "mozzarella",
        "parmesan",
        "sour cream",
        "yoghurt",
    }
    assert r.usage.requests == 1 and not dairy & {i.name for i in r.items}


def test_d_rerun_a_is_a_cache_hit(client, cache):
    r = go(client, cache, "(a) again: should be all cache hits", [Meal(**FRIDAY)])
    assert r.usage.requests == 0 and r.cache.hits == 3 and r.cache.misses == 0
