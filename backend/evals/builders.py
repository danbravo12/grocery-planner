"""Planner eval inputs: saved price-agent fixtures, scenarios built from them, or a small
constructed basket. No API calls. Reuses the test fixtures (tests/fixtures, tests/helpers)."""

from datetime import date
from typing import Any

from grocery_planner.domain import PricedItem
from tests import fixtures
from tests.helpers import BASKET_D, COLES, WOOL, not_found, priced

SCENARIOS = {
    "large_gap": fixtures.large_gap_scenario,
    "split": fixtures.split_scenario,
    "special_tomorrow": fixtures.special_tomorrow_scenario,
    "doubtful_match": fixtures.doubtful_match_scenario,
}
STORES = {"coles": COLES, "woolworths": WOOL}


def build_rows(spec: dict[str, Any]) -> tuple[list[PricedItem], date]:
    """spec.kind is:
    - fixture:  name: a|b|c (a saved price-agent output)
    - scenario: name: large_gap|split|special_tomorrow|doubtful_match (built from fixture b)
    - basket_d: the near-identical 8-item basket (edge case d), optionally with
        coles_factor / wool_factor (scale every price at that store),
        prices: {item_id: {store: "9.50"}} (override one price),
        missing: {item_id: [store]} (that store has no product for the item)
    """
    kind = spec["kind"]
    if kind == "fixture":
        f = fixtures.load_price_fixture(spec["name"])
        return list(f.items), f.as_of
    if kind == "scenario":
        f = SCENARIOS[spec["name"]]()
        return list(f.items), f.as_of
    if kind == "basket_d":
        return _basket_d(spec), date(2026, 10, 12)
    raise ValueError(f"unknown planner input kind '{kind}'")


def _basket_d(spec: dict[str, Any]) -> list[PricedItem]:
    prices: dict[str, dict[str, str]] = spec.get("prices", {})
    missing: dict[str, list[str]] = spec.get("missing", {})
    factors = {COLES: spec.get("coles_factor"), WOOL: spec.get("wool_factor")}
    prefixes = {COLES: "col", WOOL: "wol"}
    rows: list[PricedItem] = []
    for item_id, term, slug in BASKET_D:
        for store in (COLES, WOOL):
            if store.value in missing.get(item_id, []):
                rows.append(not_found(item_id, store, term))
                continue
            override = prices.get(item_id, {}).get(store.value)
            rows.append(
                priced(
                    item_id,
                    store,
                    term,
                    f"{prefixes[store]}-{slug}-01",
                    price=override,
                    factor=None if override else factors[store],
                )  # fmt: skip
            )
    return rows


def item_names(rows: list[PricedItem]) -> dict[str, str]:
    return {r.item_id: r.item_name for r in rows}
