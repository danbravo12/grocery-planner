"""Deterministic maths for the planner agent. Pure functions, any number of stores.

Everything stays in full-precision Decimal; use format_money only for display.
"""

from collections.abc import Sequence
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from pydantic import BaseModel

from grocery_planner.domain import PlannerConfig, PricedItem, Store
from grocery_planner.tools.price_lookup import specials_ending_within

USABLE_TIERS = {"exact", "equivalent"}
SPECIAL_WINDOW_DAYS = 7


# ---- result models -------------------------------------------------------------------------


class StoreCoverage(BaseModel):
    matched: int
    total: int
    pct: Decimal


class Coverage(BaseModel):
    total_items: int
    per_store: dict[Store, StoreCoverage]
    unmatched_everywhere: list[str]
    missing_by_store: dict[Store, list[str]]


class ItemSaving(BaseModel):
    item_id: str
    item_name: str
    costs: dict[Store, Decimal]
    cheapest_store: Store
    saving: Decimal  # most expensive store's cost minus the cheapest's
    tier_gap_flag: bool
    special_flag: bool


class SplitResult(BaseModel):
    assignment: dict[str, Store]  # every common item, with defaults filled in
    total: Decimal
    cheapest_single_store: Store
    cheapest_single_total: Decimal
    savings: Decimal  # cheapest_single_total - total


class Verdict(BaseModel):
    kind: Literal["no_meaningful_difference", "one_store", "split"]
    store: Store | None  # set for one_store
    assignment: dict[str, Store] | None  # set for split
    totals: dict[Store, Decimal]
    cheapest_store: Store
    gap: Decimal  # second-cheapest total minus cheapest total
    threshold: Decimal
    split_total: Decimal
    split_savings: Decimal  # over the cheapest single store
    item_savings: list[ItemSaving]


# ---- helpers -------------------------------------------------------------------------------


def format_money(amount: Decimal) -> str:
    return f"${amount.quantize(Decimal('0.01'), ROUND_HALF_UP):,.2f}"


def is_usable(item: PricedItem) -> bool:
    return item.match_tier in USABLE_TIERS


def _by_item(
    items: Sequence[PricedItem], stores: Sequence[Store]
) -> dict[str, dict[Store, PricedItem]]:
    """item_id -> store -> row, for the given stores. Keeps first-seen item order."""
    out: dict[str, dict[Store, PricedItem]] = {}
    for it in items:
        if it.store not in stores:
            continue
        rows = out.setdefault(it.item_id, {})
        if it.store in rows:
            raise ValueError(f"duplicate row for {it.item_id} at {it.store.value}")
        rows[it.store] = it
    return out


def _usable(rows: dict[Store, PricedItem], store: Store) -> PricedItem | None:
    row = rows.get(store)
    return row if row is not None and is_usable(row) else None


def _cost(row: PricedItem) -> Decimal:
    assert row.total_cost is not None  # guaranteed for usable rows by PricedItem validation
    return row.total_cost


# ---- coverage and stop rules ---------------------------------------------------------------


def coverage(items: Sequence[PricedItem], stores: Sequence[Store]) -> Coverage:
    by_item = _by_item(items, stores)
    total = len(by_item)
    missing = {s: [i for i, rows in by_item.items() if _usable(rows, s) is None] for s in stores}
    per_store = {
        s: StoreCoverage(
            matched=total - len(missing[s]),
            total=total,
            pct=Decimal(total - len(missing[s])) / total if total else Decimal(0),
        )
        for s in stores
    }
    nowhere = [i for i in by_item if all(i in missing[s] for s in stores)]
    return Coverage(
        total_items=total,
        per_store=per_store,
        unmatched_everywhere=nowhere,
        missing_by_store=missing,
    )


def should_stop(cov: Coverage, config: PlannerConfig) -> tuple[bool, list[str]]:
    """Stop when too much is missing. Exactly at a threshold does not stop."""
    if cov.total_items == 0:
        return True, ["no items to compare"]
    reasons = []
    nowhere_pct = Decimal(len(cov.unmatched_everywhere)) / cov.total_items
    if nowhere_pct > config.stop_not_found_pct:
        reasons.append(
            f"{len(cov.unmatched_everywhere)} of {cov.total_items} items have no usable match "
            f"at any store ({nowhere_pct:.0%} > {config.stop_not_found_pct:.0%})"
        )
    for store, sc in cov.per_store.items():
        if sc.pct < config.stop_store_matched_pct:
            reasons.append(
                f"{store.value} matched {sc.matched} of {sc.total} items "
                f"({sc.pct:.0%} < {config.stop_store_matched_pct:.0%})"
            )
    return bool(reasons), reasons


# ---- baskets and savings -------------------------------------------------------------------


def common_items(items: Sequence[PricedItem], stores: Sequence[Store]) -> list[str]:
    """Item ids with a usable match at every store."""
    return [
        i for i, rows in _by_item(items, stores).items() if all(_usable(rows, s) for s in stores)
    ]


def basket_totals(items: Sequence[PricedItem], stores: Sequence[Store]) -> dict[Store, Decimal]:
    """Per-store total on common items only."""
    by_item = _by_item(items, stores)
    common = common_items(items, stores)
    return {s: sum((_cost(by_item[i][s]) for i in common), Decimal(0)) for s in stores}


def per_item_savings(
    items: Sequence[PricedItem], stores: Sequence[Store], as_of: date
) -> list[ItemSaving]:
    """One entry per common item, biggest saving first (ties by item id)."""
    by_item = _by_item(items, stores)
    out = []
    for item_id in common_items(items, stores):
        picks = {s: by_item[item_id][s] for s in stores}
        costs = {s: _cost(p) for s, p in picks.items()}
        cheapest = min(stores, key=lambda s: costs[s])  # ties: first store in `stores`
        rows = list(picks.values())
        out.append(
            ItemSaving(
                item_id=item_id,
                item_name=rows[0].item_name,
                costs=costs,
                cheapest_store=cheapest,
                saving=max(costs.values()) - costs[cheapest],
                tier_gap_flag=len({p.brand_label for p in rows}) > 1
                or len({p.match_tier for p in rows}) > 1,
                special_flag=bool(
                    specials_ending_within([picks[cheapest]], as_of, SPECIAL_WINDOW_DAYS)
                ),
            )
        )
    return sorted(out, key=lambda x: (-x.saving, x.item_id))


def _cheapest_single(totals: dict[Store, Decimal], stores: Sequence[Store]) -> Store:
    return min(stores, key=lambda s: totals[s])  # ties: first store in `stores`


def what_if_split(
    items: Sequence[PricedItem], stores: Sequence[Store], assignment: dict[str, Store]
) -> SplitResult:
    """Total if the assigned items come from the given stores and the rest from the cheapest
    single store. Raises if an item is not common or not usable at its assigned store."""
    by_item = _by_item(items, stores)
    common = common_items(items, stores)
    totals = basket_totals(items, stores)
    single = _cheapest_single(totals, stores)
    for item_id, store in assignment.items():
        if item_id not in common:
            raise ValueError(f"{item_id} is not a common item")
        if store not in stores or _usable(by_item[item_id], store) is None:
            raise ValueError(f"{item_id} has no usable match at {store.value}")
    full = {i: assignment.get(i, single) for i in common}
    total = sum((_cost(by_item[i][s]) for i, s in full.items()), Decimal(0))
    return SplitResult(
        assignment=full,
        total=total,
        cheapest_single_store=single,
        cheapest_single_total=totals[single],
        savings=totals[single] - total,
    )


def best_split(items: Sequence[PricedItem], stores: Sequence[Store]) -> SplitResult:
    """Every common item from its cheapest store. Ties go to the cheapest single store, so a tie
    never creates a trip to another store."""
    by_item = _by_item(items, stores)
    single = _cheapest_single(basket_totals(items, stores), stores)
    assignment = {}
    for item_id in common_items(items, stores):
        costs = {s: _cost(by_item[item_id][s]) for s in stores}
        low = min(costs.values())
        assignment[item_id] = (
            single if costs[single] == low else min(stores, key=lambda s: costs[s])
        )
    return what_if_split(items, stores, assignment)


def savings_threshold(basket_total: Decimal, config: PlannerConfig) -> Decimal:
    return max(config.savings_threshold_dollars, config.savings_threshold_pct * basket_total)


def rule_verdict(
    items: Sequence[PricedItem], stores: Sequence[Store], config: PlannerConfig, as_of: date
) -> Verdict:
    """Apply the decision rule. Meeting the threshold exactly counts as meeting it."""
    if len(stores) < 2:
        raise ValueError("need at least two stores to compare")
    if not common_items(items, stores):
        raise ValueError("no common items to compare")
    totals = basket_totals(items, stores)
    ranked = sorted(stores, key=lambda s: totals[s])  # stable: ties keep `stores` order
    cheapest = ranked[0]
    gap = totals[ranked[1]] - totals[cheapest]
    threshold = savings_threshold(totals[cheapest], config)
    split = best_split(items, stores)
    if split.savings >= threshold:
        kind, store, assignment = "split", None, split.assignment
    elif gap >= threshold:
        kind, store, assignment = "one_store", cheapest, None
    else:
        kind, store, assignment = "no_meaningful_difference", None, None
    return Verdict(
        kind=kind,
        store=store,
        assignment=assignment,
        totals=totals,
        cheapest_store=cheapest,
        gap=gap,
        threshold=threshold,
        split_total=split.total,
        split_savings=split.savings,
        item_savings=per_item_savings(items, stores, as_of),
    )
