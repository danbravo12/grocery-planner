import re
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from grocery_planner.config import ACTIVE_STORES
from grocery_planner.domain import CatalogueProduct, Store
from grocery_planner.tools.price_lookup import (
    DEFAULT_SEED_PATH,
    SeededFilePriceSource,
    load_seed_products,
    pack_gap_exceeds,
    packs_needed,
    sale_week_end,
    special_active,
    specials_ending_within,
)
from grocery_planner.tools.seed_sql import seed_sql

ANCHOR = date(2026, 10, 14)  # Wednesday: the sale week ends Tue 2026-10-20; edge cases use it
EDGE_MD = DEFAULT_SEED_PATH.parent / "EDGE_CASES.md"


@pytest.fixture(scope="module")
def products() -> dict[str, CatalogueProduct]:
    return {p.product_id: p for p in load_seed_products(anchor=ANCHOR)}


def test_seed_validates_and_is_sane(products):
    assert len(products) >= 250
    ingredients = {p.search_terms[0] for p in products.values()}
    assert len(ingredients) >= 100
    for store in ACTIVE_STORES:
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


def test_b_purchased_quantities_across_stores(products):
    def purchased(pid, needed):
        p = products[pid]
        return packs_needed(Decimal(needed), p.pack_unit, p)[0] * p.pack_size

    # needing 400 g of spaghetti: one 500 g pack vs one 450 g pack (11% apart): within
    spag = [purchased("col-spaghetti-01", 400), purchased("wol-spaghetti-01", 400)]
    assert spag == [500, 450] and not pack_gap_exceeds(spag)
    # needing 1 kg of yoghurt: one 1000 g pack vs two 700 g packs (1400 g, 40% apart): beyond
    yog = [purchased("col-yoghurt-01", 1000), purchased("wol-yoghurt-01", 1000)]
    assert yog == [1000, 1400] and pack_gap_exceeds(yog)


def test_count_based_packs_have_their_typical_count(products):
    for store in ("col", "wol"):
        assert products[f"{store}-spring-onion-01"].pack_size == 6
        assert products[f"{store}-celery-01"].pack_size == 10
        assert products[f"{store}-corn-cobs-01"].pack_size == 4
        assert products[f"{store}-eggs-01"].pack_size == 12


def test_c_special_ending_mid_week(products):
    p = products["wol-chicken-breast-01"]
    assert p.on_special and p.special_end_date is not None
    assert p.special_end_date == date(2026, 10, 20)  # the Tuesday ending the sale week
    assert p.special_end_date.strftime("%A") == "Tuesday"
    assert p.regular_price == Decimal("11.50") and p.price == Decimal("8.50")
    assert special_active(p, ANCHOR) and not special_active(p, p.special_end_date + timedelta(1))
    ending = specials_ending_within(list(products.values()), ANCHOR, 6)
    assert sorted(x.product_id for x in ending) == ["col-bacon-02", "wol-chicken-breast-01"]


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
    assert "'2026-10-20'" in sql and "'2026-10-27'" in sql
    assert "regular_price" in sql and "11.50" in sql
    assert sql.count("INSERT INTO price_catalogue") == len(products)
    assert sql.startswith("BEGIN;") and sql.rstrip().endswith("COMMIT;")


def test_edge_doc_exists():
    assert Path(EDGE_MD).exists()


def test_relative_dates_follow_the_anchor():
    later = {p.product_id: p for p in load_seed_products(anchor=date(2027, 1, 4))}  # a Monday
    assert later["wol-chicken-breast-01"].special_end_date == date(2027, 1, 5)  # sale week Dec 30
    assert later["col-fries-02"].special_end_date == date(2027, 1, 12)  # second sale week
    assert later["wol-bacon-01"].price_timestamp.date() == date(2027, 1, 4)


def test_default_anchor_is_today_in_sydney():
    from grocery_planner.tools.price_lookup import today_sydney

    default = {p.product_id: p for p in load_seed_products()}
    assert default["wol-chicken-breast-01"].special_end_date == sale_week_end(today_sydney())


def test_active_stores_all_have_seed_products_and_aldi_is_inactive(products):
    seeded = {p.store for p in products.values()}
    assert set(ACTIVE_STORES) <= seeded
    assert Store.ALDI not in ACTIVE_STORES and Store.ALDI not in seeded


# ---- the weekly special cycle (Wednesday to Tuesday) ---------------------------------------------


@pytest.mark.parametrize(
    ("anchor", "week_1", "week_2"),
    [
        (date(2026, 10, 14), date(2026, 10, 20), date(2026, 10, 27)),  # Wednesday: day one
        (date(2026, 10, 15), date(2026, 10, 20), date(2026, 10, 27)),  # Thursday
        (date(2026, 10, 19), date(2026, 10, 20), date(2026, 10, 27)),  # Monday: ends tomorrow
        (date(2026, 10, 20), date(2026, 10, 20), date(2026, 10, 27)),  # Tuesday: ends today
        (date(2026, 10, 21), date(2026, 10, 27), date(2026, 11, 3)),  # next Wednesday: new week
        (date(2026, 10, 12), date(2026, 10, 13), date(2026, 10, 20)),  # Monday of the week before
    ],
)
def test_sale_week_runs_wednesday_to_tuesday(anchor, week_1, week_2):
    assert sale_week_end(anchor, 1) == week_1 and sale_week_end(anchor, 2) == week_2
    assert week_1.weekday() == 1 and (week_1 - anchor).days in range(
        0, 7
    )  # a Tuesday, within 6 days


def test_special_weeks_zero_is_rejected():
    with pytest.raises(ValueError):
        sale_week_end(ANCHOR, 0)


def test_every_special_has_a_regular_price_above_its_special_price(products):
    specials = [p for p in products.values() if p.on_special]
    assert {  # the original three; the second batch of ingredients adds more (test_seed_expansion)
        "wol-chicken-breast-01",
        "col-bacon-02",
        "col-fries-02",
    } <= {p.product_id for p in specials}
    for p in specials:
        assert p.regular_price is not None and p.regular_price > p.price
        assert p.special_end_date in (date(2026, 10, 20), date(2026, 10, 27))
    assert all(p.regular_price is None for p in products.values() if not p.on_special)
    assert products["col-fries-02"].special_end_date == date(2026, 10, 27)  # special_weeks 2


def test_regular_price_is_validated():
    base = next(p for p in load_seed_products(anchor=ANCHOR) if p.product_id == "col-bacon-02")
    data = base.model_dump(exclude={"unit_price"})
    for bad in (
        {"regular_price": Decimal("4.50")},  # not above the special price
        {"regular_price": None},  # a special needs one
        {"on_special": False, "special_end_date": None},  # a regular price needs a special
    ):
        with pytest.raises(ValueError):
            CatalogueProduct.model_validate({**data, **bad})
    CatalogueProduct.model_validate(data)
