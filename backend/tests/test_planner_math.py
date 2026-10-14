from decimal import Decimal

import pytest
from pydantic import ValidationError

from grocery_planner.config import ACTIVE_STORES
from grocery_planner.domain import PlannerConfig, PricedItem, Store
from grocery_planner.tools.planner_math import (
    basket_totals,
    best_split,
    common_items,
    coverage,
    format_money,
    per_item_savings,
    rule_verdict,
    savings_threshold,
    should_stop,
    what_if_split,
)
from tests.helpers import AS_OF, COLES, WOOL, basket_d, fillet, not_found, priced, synthetic

CFG = PlannerConfig()
BOTH = list(ACTIVE_STORES)


def D(x: str) -> Decimal:
    return Decimal(x)


# ---- models -----------------------------------------------------------------------------


def test_config_defaults():
    c = PlannerConfig()
    assert (c.savings_threshold_dollars, c.savings_threshold_pct) == (D("8"), D("0.08"))
    assert (c.stop_not_found_pct, c.stop_comparable_pct, c.pack_size_tolerance) == (
        D("0.25"), D("0.50"), D("0.15"),
    )  # fmt: skip


@pytest.mark.parametrize(
    "field, ok_lo, ok_hi, bad_lo, bad_hi",
    [
        ("savings_threshold_dollars", "3", "25", "2.99", "25.01"),
        ("savings_threshold_pct", "0.03", "0.20", "0.029", "0.21"),
        ("stop_not_found_pct", "0.10", "0.40", "0.09", "0.41"),
        ("stop_comparable_pct", "0.40", "0.80", "0.39", "0.81"),
        ("pack_size_tolerance", "0.05", "0.25", "0.04", "0.26"),
    ],
)
def test_config_ranges_are_inclusive_and_not_clamped(field, ok_lo, ok_hi, bad_lo, bad_hi):
    assert getattr(PlannerConfig(**{field: ok_lo}), field) == D(ok_lo)
    assert getattr(PlannerConfig(**{field: ok_hi}), field) == D(ok_hi)
    for bad in (bad_lo, bad_hi):
        with pytest.raises(ValidationError):
            PlannerConfig(**{field: bad})


def test_not_found_rows_have_no_product_fields():
    row = not_found("x", COLES)
    assert row.total_cost is None and row.price is None
    with pytest.raises(ValidationError):
        PricedItem(item_id="x", item_name="x", category="x", store=COLES, price=D("1"),
                   match_tier="not_found", match_confidence="low")  # fmt: skip


def test_matched_rows_need_product_fields():
    with pytest.raises(ValidationError):
        PricedItem(item_id="x", item_name="x", category="x", store=COLES,
                   match_tier="exact", match_confidence="high")  # fmt: skip


def test_priced_item_from_seed_uses_packs_actually_bought():
    row = priced("spag", WOOL, "spaghetti", "wol-spaghetti-01")  # 450g pack, $1.50
    assert (row.packs_needed, row.total_cost, row.unit_price) == (1, D("1.50"), D("0.3333"))


# ---- thresholds -------------------------------------------------------------------------


def test_threshold_dollars_win_on_small_basket_and_pct_on_big():
    assert savings_threshold(D("50"), CFG) == D("8")  # 8% = 4.00
    assert savings_threshold(D("100"), CFG) == D("8")  # 8% = 8.00 exactly
    assert savings_threshold(D("200"), CFG) == D("16.00")
    assert savings_threshold(D("200"), PlannerConfig(savings_threshold_pct="0.03")) == D("8")


def test_format_money_rounds_only_for_display():
    assert format_money(D("12.345")) == "$12.35"
    assert format_money(D("1234.5")) == "$1,234.50"


# ---- coverage / stop --------------------------------------------------------------------


def rows(n_items: int, unmatched_everywhere: int = 0, wool_missing: int = 0) -> list[PricedItem]:
    """Items i0..; the first `unmatched_everywhere` are unusable at both stores, the next
    `wool_missing` only at Woolworths."""
    out = []
    for n in range(n_items):
        both_gone = n < unmatched_everywhere
        wool_gone = both_gone or n < unmatched_everywhere + wool_missing
        out.append(synthetic(f"i{n}", COLES, "5", "not_found" if both_gone else "equivalent"))
        out.append(synthetic(f"i{n}", WOOL, "5", "not_found" if wool_gone else "equivalent"))
    return out


def test_coverage_numbers():
    cov = coverage(rows(10, unmatched_everywhere=1, wool_missing=2), BOTH)
    assert cov.total_items == 10
    assert cov.common_items == [f"i{n}" for n in range(3, 10)]
    assert (cov.per_store[COLES].matched, cov.per_store[COLES].pct) == (9, D("0.9"))
    assert (cov.per_store[WOOL].matched, cov.per_store[WOOL].pct) == (7, D("0.7"))
    assert cov.unmatched_everywhere == ["i0"]
    assert cov.missing_by_store[WOOL] == ["i0", "i1", "i2"]


def test_should_stop_not_found_boundary():
    at = coverage(rows(4, unmatched_everywhere=1), BOTH)  # exactly 25%
    assert should_stop(at, CFG) == (False, [])
    above = coverage(rows(100, unmatched_everywhere=26), BOTH)  # 26%
    stop, reasons = should_stop(above, CFG)
    assert stop and "26 of 100" in reasons[0]


def test_should_stop_comparable_boundary():
    at = coverage(rows(10, wool_missing=5), BOTH)  # 5 of 10 usable at both stores = 50%
    assert should_stop(at, CFG) == (False, [])
    below = coverage(rows(10, wool_missing=6), BOTH)  # 4 of 10
    stop, reasons = should_stop(below, CFG)
    assert stop and reasons[0].startswith("too few comparable items: only 4 of 10")


def test_should_stop_when_stores_cover_disjoint_items():
    items = []
    for n in range(1, 11):  # Coles usable on 1-5, Woolworths on 6-10
        items.append(synthetic(f"i{n}", COLES, "5", "equivalent" if n <= 5 else "not_found"))
        items.append(synthetic(f"i{n}", WOOL, "5", "equivalent" if n > 5 else "not_found"))
    cov = coverage(items, BOTH)
    assert cov.per_store[COLES].pct == cov.per_store[WOOL].pct == D("0.5")  # reported only
    assert cov.unmatched_everywhere == [] and cov.common_items == []
    assert should_stop(cov, CFG) == (
        True,
        ["no comparable items: only 0 of 10 items are usable at every store (0% < 50%)"],
    )


@pytest.mark.parametrize("wool_missing", range(11))
def test_store_below_comparable_threshold_always_stops(wool_missing):
    # Per-store pct and the comparability rule share one denominator (all distinct items),
    # and common <= every store's matched, so a store under the threshold forces a stop.
    cov = coverage(rows(10, wool_missing=wool_missing), BOTH)
    assert all(sc.total == cov.total_items for sc in cov.per_store.values())
    store_below = any(sc.pct < CFG.stop_comparable_pct for sc in cov.per_store.values())
    assert len(cov.common_items) <= min(sc.matched for sc in cov.per_store.values())
    if store_below:
        assert should_stop(cov, CFG)[0] is True
    else:
        assert should_stop(cov, CFG) == (False, [])


def test_should_stop_respects_config_and_empty_input():
    cov = coverage(rows(4, unmatched_everywhere=1), BOTH)
    assert should_stop(cov, PlannerConfig(stop_not_found_pct="0.10"))[0] is True
    assert should_stop(coverage([], BOTH), CFG)[0] is True


def test_substitutes_are_unmatched_and_excluded_from_totals():
    items = [
        synthetic("a", COLES, "5"), synthetic("a", WOOL, "6"),
        synthetic("b", COLES, "7"), synthetic("b", WOOL, "9", "substitute"),
    ]  # fmt: skip
    cov = coverage(items, BOTH)
    assert cov.per_store[WOOL].matched == 1 and cov.missing_by_store[WOOL] == ["b"]
    assert cov.unmatched_everywhere == []  # still usable at Coles
    assert common_items(items, BOTH) == ["a"]
    assert basket_totals(items, BOTH) == {COLES: D("5"), WOOL: D("6")}


# ---- edge case a ------------------------------------------------------------------------


def test_item_at_one_store_only_is_not_common():
    items = basket_d() + [
        priced("sourdough", WOOL, "sourdough", "wol-sourdough-01"),
        not_found("sourdough", COLES, "sourdough"),
        priced("gems", COLES, "potato gems", "col-potato-gems-01"),
        # no Woolworths row at all for gems
    ]
    common = common_items(items, BOTH)
    assert "sourdough" not in common and "gems" not in common and len(common) == 8
    cov = coverage(items, BOTH)
    assert cov.missing_by_store[COLES] == ["sourdough"]
    assert cov.missing_by_store[WOOL] == ["gems"]
    assert cov.unmatched_everywhere == []
    assert basket_totals(items, BOTH) == basket_totals(basket_d(), BOTH)


# ---- verdict scenarios ------------------------------------------------------------------


def test_edge_d_near_identical_basket_is_no_meaningful_difference():
    v = rule_verdict(basket_d(), BOTH, CFG, AS_OF)
    assert v.kind == "no_meaningful_difference" and v.store is None and v.assignment is None
    assert v.totals == {COLES: D("34.80"), WOOL: D("34.50")}
    assert (v.gap, v.threshold, v.split_savings) == (D("0.30"), D("8"), D("0.50"))
    assert v.cheapest_store == WOOL


def test_large_gap_is_one_store():
    v = rule_verdict(basket_d(coles_factor="1.25"), BOTH, CFG, AS_OF)  # Coles 25% dearer
    assert v.kind == "one_store" and v.store == WOOL
    assert v.totals[WOOL] == D("34.50") and v.totals[COLES] > D("43")
    assert v.gap >= v.threshold == D("8") and v.split_savings == 0


def test_one_expensive_item_saving_below_threshold_is_not_a_split():
    # Woolworths 50% dearer on the basket, but fillet is $4.50 cheaper there.
    items = basket_d(wool_factor="1.5") + fillet("26.00")
    v = rule_verdict(items, BOTH, CFG, AS_OF)
    assert v.kind == "one_store" and v.store == COLES
    assert v.totals == {COLES: D("60.80"), WOOL: D("73.25")}
    assert (v.gap, v.threshold, v.split_savings) == (D("12.45"), D("8"), D("4.50"))
    top = v.item_savings[0]
    assert (top.item_id, top.cheapest_store, top.saving) == ("fillet", WOOL, D("4.50"))


def test_same_item_saving_above_threshold_is_a_split():
    items = basket_d(wool_factor="1.5") + fillet("34.00")
    v = rule_verdict(items, BOTH, CFG, AS_OF)
    assert v.kind == "split"
    assert v.totals[COLES] == D("68.80")
    assert (v.threshold, v.split_savings, v.split_total) == (D("8"), D("12.50"), D("56.30"))
    assert v.assignment["fillet"] == WOOL
    assert all(s == COLES for i, s in v.assignment.items() if i != "fillet")


def test_split_threshold_is_inclusive():
    items = basket_d(wool_factor="1.5") + fillet("29.50")  # saves exactly 8.00
    v = rule_verdict(items, BOTH, CFG, AS_OF)
    assert v.split_savings == v.threshold == D("8.00") and v.kind == "split"


def test_gap_threshold_is_inclusive():
    items = [synthetic("a", COLES, "100"), synthetic("a", WOOL, "108")]
    v = rule_verdict(items, BOTH, CFG, AS_OF)  # gap 8.00 == max(8, 8% of 100)
    assert (v.gap, v.threshold, v.kind, v.store) == (D("8"), D("8.00"), "one_store", COLES)
    just_under = [synthetic("a", COLES, "100"), synthetic("a", WOOL, "107.99")]
    assert rule_verdict(just_under, BOTH, CFG, AS_OF).kind == "no_meaningful_difference"


def test_percentage_threshold_wins_on_big_basket():
    items = [synthetic("a", COLES, "200"), synthetic("a", WOOL, "215")]
    v = rule_verdict(items, BOTH, CFG, AS_OF)  # gap 15 > $8 but < 8% of 200 = 16
    assert v.threshold == D("16.00") and v.kind == "no_meaningful_difference"


def test_verdict_errors():
    with pytest.raises(ValueError):
        rule_verdict(basket_d(), [COLES], CFG, AS_OF)
    with pytest.raises(ValueError):
        rule_verdict([synthetic("a", COLES, "5"), not_found("a", WOOL)], BOTH, CFG, AS_OF)


# ---- what-if and best split -------------------------------------------------------------


def test_what_if_split_defaults_unassigned_to_cheapest_single_store():
    items = basket_d()
    r = what_if_split(items, BOTH, {"mince": COLES})
    assert r.cheapest_single_store == WOOL and r.cheapest_single_total == D("34.50")
    assert r.assignment["mince"] == COLES and r.assignment["milk"] == WOOL
    assert r.total == D("34.20") and r.savings == D("0.30")  # mince 6.50 instead of 6.80


def test_what_if_split_rejects_unusable_assignment():
    items = basket_d() + [priced("x", COLES, "bacon", "col-bacon-01"), not_found("x", WOOL)]
    with pytest.raises(ValueError):
        what_if_split(items, BOTH, {"x": WOOL})  # not usable at Woolworths (and not common)
    sub = [synthetic("a", COLES, "5"), synthetic("a", WOOL, "6", "substitute")]
    with pytest.raises(ValueError):
        what_if_split(sub, BOTH, {"a": WOOL})
    with pytest.raises(ValueError):
        what_if_split(basket_d(), BOTH, {"milk": Store.ALDI})


def test_best_split_ties_stay_with_cheapest_single_store():
    items = [
        synthetic("a", COLES, "5"),
        synthetic("a", WOOL, "5"),
        synthetic("b", COLES, "9"),
        synthetic("b", WOOL, "10"),
    ]
    r = best_split(items, BOTH)
    assert r.assignment == {"a": COLES, "b": COLES} and r.savings == 0


# ---- per-item savings, flags (edge cases c and f) ---------------------------------------


def test_edge_c_special_ending_midweek_sets_special_flag():
    items = [
        priced("chicken", COLES, "chicken breast", "col-chicken-breast-01"),
        priced("chicken", WOOL, "chicken breast", "wol-chicken-breast-01"),
    ]
    (s,) = per_item_savings(items, BOTH, AS_OF)
    assert s.cheapest_store == WOOL and s.costs == {COLES: D("11.00"), WOOL: D("8.50")}
    assert s.saving == D("2.50") and s.special_flag is True and s.tier_gap_flag is False
    (after,) = per_item_savings(items, BOTH, AS_OF.replace(day=16))  # special has ended
    assert after.special_flag is False


def test_special_flag_needs_end_within_seven_days():
    items = [priced("chicken", COLES, "chicken breast", "col-chicken-breast-01"),
             priced("chicken", WOOL, "chicken breast", "wol-chicken-breast-01")]  # fmt: skip
    assert per_item_savings(items, BOTH, AS_OF.replace(day=8))[0].special_flag is True  # exactly 7
    assert per_item_savings(items, BOTH, AS_OF.replace(day=7))[0].special_flag is False  # 8 days


def test_edge_f_branded_special_beats_house_brand_with_tier_gap():
    items = [
        priced("bacon", COLES, "bacon", "col-bacon-02"),  # Kingsway, on special, $4.50
        priced("bacon", WOOL, "bacon", "wol-bacon-01"),  # Woolworths house brand, $5.80
    ]
    (s,) = per_item_savings(items, BOTH, AS_OF)
    assert s.cheapest_store == COLES and s.saving == D("1.30")
    assert s.tier_gap_flag is True  # branded vs house_brand
    assert s.special_flag is True  # ends 6 days after anchor


def test_tier_gap_flag_on_match_tier_difference():
    items = [priced("m", COLES, "milk", "col-milk-01", tier="exact"),
             priced("m", WOOL, "milk", "wol-milk-01", tier="equivalent")]  # fmt: skip
    assert per_item_savings(items, BOTH, AS_OF)[0].tier_gap_flag is True


def test_per_item_savings_sorted_by_saving_desc():
    s = per_item_savings(basket_d(wool_factor="1.5"), BOTH, AS_OF)
    assert [x.saving for x in s] == sorted((x.saving for x in s), reverse=True)


# ---- N stores ---------------------------------------------------------------------------


def test_three_store_support():
    A = Store.ALDI
    stores = [COLES, WOOL, A]
    items = []
    for item_id, costs in {
        "a": ("10", "12", "9"),
        "b": ("40", "15", "41"),
        "c": ("20", "31", "40"),
    }.items():
        items += [synthetic(item_id, s, c) for s, c in zip(stores, costs, strict=True)]
    items += [
        synthetic("d", COLES, "5"),
        not_found("d", WOOL),
        synthetic("d", A, "5"),
    ]  # not common
    assert common_items(items, stores) == ["a", "b", "c"]
    assert basket_totals(items, stores) == {COLES: D("70"), WOOL: D("58"), A: D("90")}
    cov = coverage(items, stores)
    assert cov.missing_by_store[WOOL] == ["d"] and cov.per_store[A].matched == 4
    v = rule_verdict(items, stores, CFG, AS_OF)
    assert v.cheapest_store == WOOL and v.gap == D("12") and v.threshold == D("8")
    assert (v.split_total, v.split_savings) == (D("44"), D("14"))  # a@aldi 9, b@wool 15, c@coles 20
    assert v.kind == "split" and v.assignment == {"a": A, "b": WOOL, "c": COLES}
    assert [x.item_id for x in v.item_savings] == ["b", "c", "a"]


# ---- pack gap ---------------------------------------------------------------------------------


def test_pack_gap_flag_in_per_item_savings():
    items = [
        priced("yoghurt", COLES, "yoghurt", "col-yoghurt-01"),  # buys 1000 g
        priced("yoghurt", WOOL, "yoghurt", "wol-yoghurt-01"),  # buys 700 g: 43% apart
        priced("spag", COLES, "spaghetti", "col-spaghetti-01"),  # 500 g
        priced("spag", WOOL, "spaghetti", "wol-spaghetti-01"),  # 450 g: 11% apart
    ]
    flags = {s.item_id: s.pack_gap_flag for s in per_item_savings(items, BOTH, AS_OF)}
    assert flags == {"yoghurt": True, "spag": False}
    loose = {s.item_id: s.pack_gap_flag for s in per_item_savings(items, BOTH, AS_OF, D("0.5"))}
    assert loose == {"yoghurt": False, "spag": False}


def test_rule_verdict_uses_the_configured_pack_tolerance():
    items = [
        priced("spag", COLES, "spaghetti", "col-spaghetti-01"),
        priced("spag", WOOL, "spaghetti", "wol-spaghetti-01"),
    ]
    assert rule_verdict(items, BOTH, CFG, AS_OF).item_savings[0].pack_gap_flag is False
    tight = PlannerConfig(pack_size_tolerance="0.05")
    assert rule_verdict(items, BOTH, tight, AS_OF).item_savings[0].pack_gap_flag is True
