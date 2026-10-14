"""Needs a real Postgres (SUPABASE_DB_URL). Skipped otherwise. Note: the fixture upserts the
seed catalogue with today's anchor, which is the same state the daily seed job produces."""

from datetime import date

import psycopg
import pytest

from grocery_planner.config import ACTIVE_STORES
from grocery_planner.db import CONNECT_KWARGS, db_url
from grocery_planner.db.migrate import migrate
from grocery_planner.db.seed import seed
from grocery_planner.tools.price_lookup import (
    PostgresPriceSource,
    SeededFilePriceSource,
    today_sydney,
)

pytestmark = pytest.mark.integration

TERMS = [
    "bacon", "eye fillet", "beef mince", "chicken breast", "chicken", "milk", "eggs", "egg",
    "bread", "white bread", "pasta", "spaghetti", "tomato sauce", "tomato", "cheese", "rice",
    "oil", "yoghurt", "sourdough", "potato gems", "dragonfruit", "fries", "peas", "onion",
]  # fmt: skip
BRANDS = [None, "Kingsway", "bellvue", "Coles", "Nope"]


@pytest.fixture(scope="module")
def anchor() -> date:
    return today_sydney()


@pytest.fixture(scope="module")
def pg(anchor):
    migrate(db_url())
    seed(db_url(), anchor)
    source = PostgresPriceSource(db_url())
    yield source
    source.close()


def test_postgres_matches_seeded_source(pg, anchor):
    seeded = SeededFilePriceSource(anchor=anchor)
    checked = 0
    for store in ACTIVE_STORES:
        for term in TERMS:
            for brand in BRANDS:
                for limit in (1, 50):
                    assert pg.search(term, store, limit, brand) == seeded.search(
                        term, store, limit, brand
                    ), (term, store, brand, limit)
                    checked += 1
    assert checked == len(ACTIVE_STORES) * len(TERMS) * len(BRANDS) * 2


def test_seed_is_idempotent(anchor):
    assert seed(db_url(), anchor) == seed(db_url(), anchor)


def test_migrations_are_idempotent():
    migrate(db_url())
    assert migrate(db_url()) == []  # second run applies nothing, no error


def test_rls_enabled_on_every_table_with_no_policies():
    with psycopg.connect(db_url(), **CONNECT_KWARGS) as conn:
        tables = conn.execute(
            "SELECT tablename, rowsecurity FROM pg_tables WHERE schemaname = 'public'"
        ).fetchall()
        policies = conn.execute(
            "SELECT tablename, policyname FROM pg_policies WHERE schemaname = 'public'"
        ).fetchall()
    names = {t["tablename"] for t in tables}
    assert {"runs", "run_steps", "price_catalogue", "dish_cache", "usage_counters"} <= names
    assert [t["tablename"] for t in tables if not t["rowsecurity"]] == []
    assert policies == []


def test_dish_cache_round_trip():
    from decimal import Decimal

    from grocery_planner.db.dish_cache import PostgresDishCache
    from grocery_planner.domain.ingredients import DishIngredient, DishResult

    key = ("integration test dish", "dairy,nuts")
    result = DishResult(
        ingredients=[
            DishIngredient(
                name="butter", quantity_per_serving=Decimal("12.5"), unit="g", in_vocabulary=True
            )
        ],
        assumptions=["Swapped for oil."],
    )
    cache = PostgresDishCache(db_url())
    try:
        assert cache.get(*key) is None
        cache.put(*key, "Integration Test Dish", result, "model-a")
        assert cache.get(*key) == result
        assert cache.get(key[0], "") is None  # a different avoid is a different row
        cache.put(*key, "Integration Test Dish", result.model_copy(update={"assumptions": []}), "b")
        assert cache.get(*key).assumptions == []  # upsert overwrites
    finally:
        with psycopg.connect(db_url(), **CONNECT_KWARGS) as conn:
            conn.execute("DELETE FROM dish_cache WHERE dish_key = %s", (key[0],))
        cache.close()
