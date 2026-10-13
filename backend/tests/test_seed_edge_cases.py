import re
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from grocery_planner.domain import CatalogueProduct, Store
from grocery_planner.tools.price_lookup import (
    DEFAULT_SEED_PATH,
    SeededFilePriceSource,
    load_seed_products,
    pack_size_within_tolerance,
    special_active,
    specials_ending_within,
)
from grocery_planner.tools.seed_sql import seed_sql

ANCHOR = date(2026, 10, 12)  # Monday; edge cases are described relative to this
EDGE_MD = DEFAULT_SEED_PATH.parent / "EDGE_CASES.md"


@pytest.fixture(scope="module")
def products() -> dict[str, CatalogueProduct]:
    return {p.product_id: p for p in load_seed_products(anchor=ANCHOR)}


def test_seed_validates_and_is_sane(products):
    assert len(products) >= 250
    ingredients = {p.search_terms[0] for p in products.values()}
    assert len(ingredients) >= 100
    for store in (Store.COLES, Store.WOOLWORTHS):
        per_item: dict[str, int] = {}
        for p in products.values():
            if p.store == store:
                per_item[p.search_terms[0]] = per_item.get(p.search_terms[0], 0) + 1
        assert 1 <= min(per_item.values()) and max(per_item.values()) <= 3
    assert all(p.data_source == "seeded" for p in products.values())


def test_every_product_id_in_edge_cases_doc_exists(products):
    ids = set(re.findall(r"`((?:col|wol)-[a-z0-9-]+-\d\d)`", EDGE_MD.read_text()))
    assert ids and ids <= products.keys()


def test_a_item_missing_at_one_store():
    s = SeededFilePriceSource(anchor=ANCHOR)
    assert s.search("sourdough", Store.WOOLWORTHS, 5) and not s.search("sourdough", Store.COLES, 5)
    assert s.search("potato gems", Store.COLES, 5) and not s.search(
        "potato gems", Store.WOOLWORTHS, 5
    )


def test_b_pack_size_mismatches(products):
    col, wol = products["col-spaghetti-01"], products["wol-spaghetti-01"]
    assert (col.pack_size, wol.pack_size) == (500, 450)
    assert pack_size_within_tolerance(col.pack_size, wol.pack_size)
    col, wol = products["col-yoghurt-01"], products["wol-yoghurt-01"]
    assert (col.pack_size, wol.pack_size) == (1000, 700)
    assert not pack_size_within_tolerance(col.pack_size, wol.pack_size)


def test_c_special_ending_mid_week(products):
    p = products["wol-chicken-breast-01"]
    assert p.on_special and p.special_end_date is not None
    assert (p.special_end_date - ANCHOR).days == 3
    assert p.special_end_date.strftime("%A") == "Thursday"  # holds for this fixed anchor
    assert special_active(p, ANCHOR) and not special_active(p, p.special_end_date + timedelta(1))
    ending = specials_ending_within(list(products.values()), ANCHOR, 3)
    assert [x.product_id for x in ending] == ["wol-chicken-breast-01"]


def test_d_near_identical_basket(products):
    slugs = [
        "milk",
        "white-bread",
        "eggs",
        "banana",
        "brown-onion",
        "beef-mince",
        "cheddar",
        "carrot",
    ]
    totals = {pre: sum(products[f"{pre}-{s}-01"].price for s in slugs) for pre in ("col", "wol")}
    assert not any(products[f"{pre}-{s}-01"].on_special for pre in totals for s in slugs)
    gap = abs(totals["col"] - totals["wol"]) / max(totals.values())
    assert len(slugs) == 8 and gap < Decimal("0.02")


def test_e_branded_product_at_both_stores(products):
    col, wol = products["col-tomato-sauce-02"], products["wol-tomato-sauce-02"]
    assert col.brand == wol.brand == "Bellvue"
    assert col.brand_label == wol.brand_label == "branded"
    assert col.pack_size == wol.pack_size and {col.store, wol.store} == {
        Store.COLES,
        Store.WOOLWORTHS,
    }


def test_f_popular_brand_on_special_beats_house_brand(products):
    branded, house = products["col-bacon-02"], products["col-bacon-01"]
    assert branded.brand_label == "branded" and branded.on_special
    assert house.brand_label == "house_brand"
    assert branded.quality_band == house.quality_band and branded.pack_size == house.pack_size
    assert branded.unit_price < house.unit_price


def test_seed_sql_has_one_insert_per_product(products):
    sql = seed_sql(anchor=ANCHOR)
    assert "'2026-10-15'" in sql
    assert sql.count("INSERT INTO price_catalogue") == len(products)
    assert sql.startswith("BEGIN;") and sql.rstrip().endswith("COMMIT;")


def test_edge_doc_exists():
    assert Path(EDGE_MD).exists()


def test_relative_dates_follow_the_anchor():
    later = {p.product_id: p for p in load_seed_products(anchor=date(2027, 1, 4))}
    assert later["wol-chicken-breast-01"].special_end_date == date(2027, 1, 7)
    assert later["wol-bacon-01"].price_timestamp.date() == date(2027, 1, 4)


def test_default_anchor_is_today_in_sydney():
    from grocery_planner.tools.price_lookup import today_sydney

    default = {p.product_id: p for p in load_seed_products()}
    assert default["wol-chicken-breast-01"].special_end_date == today_sydney() + timedelta(3)
