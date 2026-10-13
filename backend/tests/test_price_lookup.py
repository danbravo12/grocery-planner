import os
from datetime import date
from decimal import Decimal

import pytest

from grocery_planner.config import ACTIVE_STORES
from grocery_planner.domain import Store
from grocery_planner.tools.price_lookup import (
    PostgresPriceSource,
    SeededFilePriceSource,
    UnitMismatchError,
    pack_size_within_tolerance,
    packs_needed,
)


@pytest.fixture(scope="module")
def source() -> SeededFilePriceSource:
    return SeededFilePriceSource(anchor=date(2026, 10, 12))


def by_id(source, term, store, pid):
    return next(p for p in source.search(term, store, 50) if p.product_id == pid)


def test_search_returns_candidates_per_store(source):
    coles = source.search("bacon", Store.COLES, 10)
    woolies = source.search("bacon", Store.WOOLWORTHS, 10)
    assert {p.product_id for p in coles} == {"col-bacon-01", "col-bacon-02", "col-bacon-03"}
    assert {p.product_id for p in woolies} == {"wol-bacon-01", "wol-bacon-02", "wol-bacon-03"}
    assert all(p.store == Store.COLES for p in coles)


def test_search_is_case_insensitive_and_matches_search_terms(source):
    ids = {p.product_id for p in source.search("  FILLET Mignon ", Store.COLES, 10)}
    assert ids == {"col-eye-fillet-01", "col-eye-fillet-03"}


def test_search_uses_whole_words(source):
    # Whole words only: a fragment of a word must not match.
    assert {p.product_id for p in source.search("egg", Store.COLES, 10)} >= {"col-eggs-01"}
    assert source.search("gg", Store.COLES, 10) == []


def test_search_orders_by_unit_price_and_applies_limit(source):
    all_hits = source.search("bacon", Store.WOOLWORTHS, 10)
    assert [p.unit_price for p in all_hits] == sorted(p.unit_price for p in all_hits)
    assert source.search("bacon", Store.WOOLWORTHS, 1) == all_hits[:1]
    assert source.search("bacon", Store.WOOLWORTHS, 0) == []


def test_missing_item_returns_empty(source):
    assert source.search("dragonfruit", Store.COLES, 5) == []
    assert source.search("sourdough", Store.COLES, 5) == []
    assert source.search("", Store.COLES, 5) == []


def test_named_brand_is_not_cut_off_by_limit(source):
    # Edge case e: Bellvue is not the cheapest tomato sauce, so limit=1 alone would drop it.
    cheapest = source.search("tomato sauce", Store.COLES, 1)
    assert cheapest[0].brand != "Bellvue"
    hit = source.search("tomato sauce", Store.COLES, 1, brand="bellvue")
    assert [p.product_id for p in hit] == ["col-tomato-sauce-02"]
    other = source.search("tomato sauce", Store.WOOLWORTHS, 1, brand=" BELLVUE ")
    assert [p.product_id for p in other] == ["wol-tomato-sauce-02"]
    assert source.search("tomato sauce", Store.COLES, 5, brand="Nope") == []


@pytest.mark.skipif(not os.getenv("SUPABASE_DB_URL"), reason="SUPABASE_DB_URL not set")
def test_postgres_source_matches_seeded_source(source):
    pg = PostgresPriceSource(os.environ["SUPABASE_DB_URL"])
    for store in ACTIVE_STORES:
        assert pg.search("bacon", store, 10) == source.search("bacon", store, 10)
        assert pg.search("tomato sauce", store, 1, "Bellvue") == source.search(
            "tomato sauce", store, 1, "Bellvue"
        )


def test_packs_needed_exact_fit(source):
    p = by_id(source, "spaghetti", Store.COLES, "col-spaghetti-01")  # 500g, $1.60
    assert packs_needed(Decimal(500), "g", p) == (1, Decimal("1.60"), Decimal(0))
    assert packs_needed(1000, "g", p) == (2, Decimal("3.20"), Decimal(0))


def test_packs_needed_rounds_up_and_reports_leftover(source):
    p = by_id(source, "spaghetti", Store.COLES, "col-spaghetti-01")
    packs, total, leftover = packs_needed(Decimal(501), "g", p)
    assert (packs, total, leftover) == (2, Decimal("3.20"), Decimal(499))
    assert packs_needed(Decimal(400), "g", p) == (1, Decimal("1.60"), Decimal(100))


def test_packs_needed_unit_mismatch_raises(source):
    p = by_id(source, "spaghetti", Store.COLES, "col-spaghetti-01")
    with pytest.raises(UnitMismatchError):
        packs_needed(Decimal(500), "ml", p)
    with pytest.raises(UnitMismatchError):
        packs_needed(2, "each", p)


def test_packs_needed_rejects_non_positive(source):
    p = by_id(source, "spaghetti", Store.COLES, "col-spaghetti-01")
    with pytest.raises(ValueError):
        packs_needed(0, "g", p)


def test_tolerance_boundary_is_inclusive_at_exactly_15_percent():
    assert pack_size_within_tolerance(Decimal(400), Decimal(460)) is True  # +15% exactly
    assert pack_size_within_tolerance(Decimal(400), Decimal(340)) is True  # -15% exactly
    assert pack_size_within_tolerance(Decimal(400), Decimal("460.01")) is False
    assert pack_size_within_tolerance(Decimal(400), Decimal("339.99")) is False
    assert pack_size_within_tolerance(400, 400) is True


def test_tolerance_is_configurable():
    assert pack_size_within_tolerance(400, 440, Decimal("0.05")) is False
    assert pack_size_within_tolerance(400, 420, Decimal("0.05")) is True


def test_decimal_arithmetic_has_no_float_drift(source):
    # 0.1 + 0.2 style drift: ten packs of a $0.10-multiple price must sum exactly.
    p = by_id(source, "baked beans", Store.COLES, "col-baked-beans-01")  # 420g, $1.10
    packs, total, leftover = packs_needed(Decimal(4200), "g", p)
    assert (packs, total, leftover) == (10, Decimal("11.00"), Decimal(0))
    assert isinstance(total, Decimal)
    assert sum([Decimal("0.10")] * 3) == Decimal("0.30")
