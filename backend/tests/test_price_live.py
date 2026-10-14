"""Live runs of the price agent (real Gemini; needs SUPABASE_DB_URL for the dish cache). Run with:
    uv run pytest -m live -s tests/test_price_live.py
The ingredient lists come from the Postgres dish cache, so after the first run they cost 0
ingredient requests. Prices use the seed catalogue at a fixed anchor of 2026-10-12."""

import time
from datetime import date
from decimal import Decimal

import pytest

from grocery_planner.agents.ingredient import run_ingredient_agent
from grocery_planner.agents.price import price_items
from grocery_planner.config import ACTIVE_STORES
from grocery_planner.db import db_url
from grocery_planner.db.dish_cache import PostgresDishCache
from grocery_planner.domain import PlannerConfig
from grocery_planner.domain.ingredients import IngredientItem
from grocery_planner.domain.request import Meal, PlanRequest
from grocery_planner.env import getenv
from grocery_planner.llm import GeminiClient
from grocery_planner.tools.planner_math import coverage, format_money, rule_verdict, should_stop
from grocery_planner.tools.price_lookup import SeededFilePriceSource

pytestmark = pytest.mark.live
AS_OF = date(2026, 10, 12)
CFG = PlannerConfig()


@pytest.fixture(scope="module")
def llm():
    if not getenv("GEMINI_API_KEY"):
        pytest.skip("GEMINI_API_KEY not set")
    return GeminiClient()


@pytest.fixture(scope="module")
def cache():
    try:
        c = PostgresDishCache(db_url())
    except RuntimeError:
        pytest.skip("SUPABASE_DB_URL not set")
    yield c
    c.close()


@pytest.fixture(scope="module")
def source():
    return SeededFilePriceSource(anchor=AS_OF)


def report(label, items, result, seconds):
    u = result.usage
    print(f"\n=== {label} ===")
    print(
        f"price agent: model calls={result.rounds} requests={u.requests} "
        f"input_tokens={u.input_tokens} output_tokens={u.output_tokens} "
        f"thinking_tokens={u.thinking_tokens} latency={seconds:.1f}s"
    )
    names = {i.item_id: f"{i.name} {i.quantity.normalize():f}{i.unit}" for i in items}
    for it in items:
        print(f"- {it.item_id} {names[it.item_id]}" + (f" [brand {it.brand}]" if it.brand else ""))
        for store in ACTIVE_STORES:
            r = next(x for x in result.items if x.item_id == it.item_id and x.store == store)
            cost = format_money(r.total_cost) if r.total_cost is not None else "-"
            product = r.product_name or "(none)"
            special = f" special->{r.special_end_date}" if r.on_special else ""
            qty = (
                f"bought={r.purchased_qty.normalize():f}{r.pack_unit} "
                f"left={r.leftover_qty.normalize():f}{r.pack_unit}"
                if r.purchased_qty is not None
                else "-"
            )
            print(
                f"    {store.value:<10} {product:<40} {r.match_tier:<10} {r.match_confidence:<6} "
                f"packs={r.packs_needed or '-'} {qty} cost={cost}{special}"
            )
            print(f"               reason: {result.reasons[it.item_id][store.value]}")
        for f in (f for f in result.flags if f.item_id == it.item_id):
            print(f"    FLAG {f.kind}: {f.message}")
    print(
        "downgrades:",
        [(d.item_id, d.store.value, d.from_tier, d.to_tier) for d in result.downgrades],
    )
    cov = coverage(result.items, ACTIVE_STORES)
    stop, reasons = should_stop(cov, CFG)
    print("coverage:", {s.value: f"{c.matched}/{c.total}" for s, c in cov.per_store.items()},
          "common:", len(cov.common_items), "stop:", stop, reasons)  # fmt: skip
    if cov.common_items:
        v = rule_verdict(result.items, ACTIVE_STORES, CFG, AS_OF)
        totals = {s.value: format_money(t) for s, t in v.totals.items()}
        print(
            f"rule_verdict: {v.kind} store={v.store.value if v.store else None} totals={totals} "
            f"gap={format_money(v.gap)} threshold={format_money(v.threshold)} "
            f"split_savings={format_money(v.split_savings)}"
        )
        if v.assignment:
            print("  split:", {k: s.value for k, s in v.assignment.items()})
    return result


def go(label, items, llm, source):
    start = time.perf_counter()
    result = price_items(items, llm, source, CFG, AS_OF)
    return report(label, items, result, time.perf_counter() - start)


def ingredients(llm, cache, meals):
    r = run_ingredient_agent(PlanRequest(meals=meals), llm, cache)
    print(
        f"\n(ingredient agent: requests={r.usage.requests} "
        f"cache hits={r.cache.hits} misses={r.cache.misses})"
    )
    return r.items


def test_a_friday_dinner(llm, cache, source):
    meal = Meal(
        meal_name="Friday dinner", mains=["fillet mignon"], sides=["potato salad", "fries"],
        people=2, days=1,
    )  # fmt: skip
    items = ingredients(llm, cache, [meal])
    result = go("(a) Friday dinner", items, llm, source)
    assert result.usage.requests in (1, 2) and len(result.items) == 2 * len(items)


def test_b_shared_onion_and_garlic(llm, cache, source):
    meals = [
        Meal(meal_name="Monday bolognese", mains=["spaghetti bolognese"], people=4, days=3),
        Meal(meal_name="Tuesday chilli", mains=["chilli con carne"], people=4, days=3),
    ]
    items = ingredients(llm, cache, meals)
    result = go("(b) bolognese and chilli", items, llm, source)
    assert result.usage.requests in (1, 2) and len(result.items) == 2 * len(items)


def test_c_edge_cases(llm, source):
    def item(n, name, qty, unit="g", brand=None):
        return IngredientItem(
            item_id=f"i{n}", name=name, quantity=Decimal(qty), unit=unit, category="x",
            meals=["m"], brand=brand, pantry_staple=False, in_vocabulary=True,
        )  # fmt: skip

    items = [
        item(1, "sourdough", 600), item(2, "potato gems", 1000), item(3, "spaghetti", 400),
        item(4, "yoghurt", 1000), item(5, "bacon", 250),
        item(6, "tomato sauce", 500, "ml", brand="Bellvue"), item(7, "chicken breast", 1000),
        item(8, "milk", 2000, "ml"), item(9, "bread", 700), item(10, "eggs", 12, "each"),
    ]  # fmt: skip
    result = go("(c) seeded edge cases", items, llm, source)
    assert len(result.items) == 20
