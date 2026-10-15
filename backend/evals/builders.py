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
    "special": fixtures.special_scenario,
    "doubtful_match": fixtures.doubtful_match_scenario,
}
STORES = {"coles": COLES, "woolworths": WOOL}


def build_rows(spec: dict[str, Any]) -> tuple[list[PricedItem], date]:
    """spec.kind is:
    - fixture:  name: a|b|c (a saved price-agent output)
    - scenario: name: large_gap|split|special|doubtful_match (built from fixture b)
    - basket_d: the near-identical 8-item basket (edge case d), optionally with
        coles_factor / wool_factor (scale every price at that store),
        prices: {item_id: {store: "9.50"}} (override one price),
        missing: {item_id: [store]} (that store has no product for the item),
        specials: {item_id: {store: {price: "2.00", ends: "2026-10-20"}}} (the seed price becomes
        the regular price)
    `as_of` overrides the date the plan is made on (default: the fixture's, else 2026-10-14).
    """
    kind = spec["kind"]
    if kind == "fixture":
        f = fixtures.load_price_fixture(spec["name"])
        rows, as_of = list(f.items), f.as_of
    elif kind == "scenario":
        f = SCENARIOS[spec["name"]]()
        rows, as_of = list(f.items), f.as_of
    elif kind == "basket_d":
        rows, as_of = _basket_d(spec), date(2026, 10, 14)
    else:
        raise ValueError(f"unknown planner input kind '{kind}'")
    if "as_of" in spec:
        as_of = date.fromisoformat(spec["as_of"])
    return rows, as_of


def _basket_d(spec: dict[str, Any]) -> list[PricedItem]:
    prices: dict[str, dict[str, str]] = spec.get("prices", {})
    missing: dict[str, list[str]] = spec.get("missing", {})
    specials: dict[str, dict[str, dict[str, str]]] = spec.get("specials", {})
    factors = {COLES: spec.get("coles_factor"), WOOL: spec.get("wool_factor")}
    prefixes = {COLES: "col", WOOL: "wol"}
    rows: list[PricedItem] = []
    for item_id, term, slug in BASKET_D:
        for store in (COLES, WOOL):
            if store.value in missing.get(item_id, []):
                rows.append(not_found(item_id, store, term))
                continue
            override = prices.get(item_id, {}).get(store.value)
            row = priced(
                item_id,
                store,
                term,
                f"{prefixes[store]}-{slug}-01",
                price=override,
                factor=None if override else factors[store],
            )
            special = specials.get(item_id, {}).get(store.value)
            if special:
                regular = row.total_cost
                row = fixtures.with_total(row, special["price"]).model_copy(
                    update={
                        "on_special": True,
                        "special_end_date": date.fromisoformat(special["ends"]),
                        "regular_price": regular / row.packs_needed,
                        "regular_total_cost": regular,
                    }
                )
            rows.append(row)
    return rows


def item_names(rows: list[PricedItem]) -> dict[str, str]:
    return {r.item_id: r.item_name for r in rows}
