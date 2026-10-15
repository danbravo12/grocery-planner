"""Deterministic checks. Each takes the case's parameters for that check and returns outcomes.

Every outcome has a check name; the runner reports a pass rate per check name. A check that
needs the agent's output is reported as failed ("not evaluated") when the agent errored.
There is no LLM judge: everything here is a comparison against the seed, the code's own
numbers or an expected value written in the case file.
"""

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from evals.avoid import avoids_in, violates
from evals.execute import (
    ANCHOR,
    E2ESubject,
    Execution,
    IngredientSubject,
    PlannerSubject,
    PriceSubject,
)
from evals.harness import Meter
from grocery_planner.agents.planner import _invented_amounts, _tip_problems, _what_if_tool
from grocery_planner.agents.price import find_candidates
from grocery_planner.config import ACTIVE_STORES
from grocery_planner.domain import Store
from grocery_planner.orchestrator import ESTIMATED_REQUESTS
from grocery_planner.tools.price_lookup import load_seed_products


@dataclass(frozen=True)
class Outcome:
    name: str
    passed: bool
    detail: str = ""


def _o(name: str, passed: bool, detail: str = "") -> Outcome:
    return Outcome(name, bool(passed), detail)


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple, set)) else [value]


# ---- ingredient -----------------------------------------------------------------------------


def _servings(subject: IngredientSubject, item) -> Decimal:
    meals = {m.meal_name: m for m in subject.request.meals}
    return sum((Decimal(meals[n].people * meals[n].days) for n in item.meals), Decimal(0))


def _ing_required_items(s: IngredientSubject, params: list[Any]) -> list[Outcome]:
    out = []
    for entry in params:
        scope = entry.get("meal") if isinstance(entry, dict) else None
        wanted = entry["any_of"] if isinstance(entry, dict) else _as_list(entry)
        have = {i.name.lower() for i in s.result.items if scope is None or scope in i.meals}
        hit = sorted(n for n in wanted if n.lower() in have)
        label = " or ".join(wanted) + (f" (in {scope})" if scope else "")
        out.append(_o("required_items", hit, f"{label}: " + (f"found {hit}" if hit else "missing")))
    return out


def _ing_ranges(s: IngredientSubject, params: dict[str, list[float]]) -> list[Outcome]:
    out = []
    items = {i.name.lower(): i for i in s.result.items}
    for name, (low, high) in params.items():
        item = items.get(name.lower())
        if item is None:
            continue  # absence is for required_items to catch
        per = item.quantity / _servings(s, item)
        ok = Decimal(str(low)) <= per <= Decimal(str(high))
        out.append(
            _o("quantity_range", ok, f"{name}: {per:.1f} {item.unit}/serving, want {low}-{high}")
        )
    return out


def _ing_forbidden_avoid(s: IngredientSubject, params: list[str]) -> list[Outcome]:
    out = []
    for avoid in params:
        scope = {m.meal_name for m in s.request.meals if avoid in avoids_in(m.avoid)}
        bad = sorted(
            i.name for i in s.result.items if violates(avoid, i.name) and set(i.meals) <= scope
        )
        out.append(
            _o(
                "avoid_respected",
                not bad,
                f"avoid {avoid}: " + (f"found {bad}" if bad else "none found"),
            )
        )
    return out


def _ing_forbidden_items(s: IngredientSubject, params: list[str]) -> list[Outcome]:
    bad = sorted(i.name for i in s.result.items if any(w.lower() in i.name.lower() for w in params))
    return [_o("forbidden_items_absent", not bad, f"found {bad}" if bad else "none found")]


def _ing_brands(s: IngredientSubject, params: dict[str, str]) -> list[Outcome]:
    items = {i.name.lower(): i for i in s.result.items}
    out = []
    for name, brand in params.items():
        item = items.get(name.lower())
        got = item.brand if item else None
        out.append(
            _o(
                "brand_when_named",
                got and got.lower() == brand.lower(),
                f"{name}: brand {got!r}, want {brand!r}",
            )
        )
    return out


def _ing_merged(s: IngredientSubject, params: dict[str, int]) -> list[Outcome]:
    items = {i.name.lower(): i for i in s.result.items}
    out = []
    for name, n in params.items():
        item = items.get(name.lower())
        got = len(item.meals) if item else 0
        out.append(_o("merged_across_meals", got >= n, f"{name}: used by {got} meals, want {n}"))
    return out


def _ing_assumptions(s: IngredientSubject, params: list[str]) -> list[Outcome]:
    text = " ".join(s.result.assumptions).lower()
    hit = [w for w in params if w.lower() in text]
    return [_o("assumptions_mention", hit, f"looked for {params}, found {hit}")]


def _ing_no_flags(s: IngredientSubject, params: Any) -> list[Outcome]:
    bad = [f"{i.name}: {f}" for i in s.result.items for f in i.flags]
    return [_o("no_unusual_amounts", not bad, "; ".join(bad) or "no sanity flags")]


def _ing_min_items(s: IngredientSubject, params: int) -> list[Outcome]:
    return [
        _o(
            "min_items",
            len(s.result.items) >= params,
            f"{len(s.result.items)} items, want >= {params}",
        )
    ]


def _ing_implicit(s: IngredientSubject, params: dict[str, Any]) -> list[Outcome]:
    out = []
    named_brands = {b.lower() for b in params.get("brands", {}).values()}
    dish_text = " ".join(d for m in s.request.meals for d in [*m.mains, *m.sides]).lower()
    stray = [
        f"{i.name} ({i.brand})"
        for i in s.result.items
        if i.brand and (i.brand.lower() not in named_brands or i.brand.lower() not in dish_text)
    ]
    out.append(
        _o(
            "brand_only_if_named",
            not stray,
            f"unnamed brands: {stray}" if stray else "no stray brands",
        )
    )
    keys = [(i.name.lower(), i.unit, (i.brand or "").lower()) for i in s.result.items]
    out.append(_o("unique_lines", len(keys) == len(set(keys)), f"{len(keys)} lines"))
    staples = [i.name for i in s.result.items if i.pantry_staple]
    ok = s.request.include_pantry_staples or not staples
    out.append(
        _o("pantry_staples_respected", ok, f"staples listed: {staples}" if staples else "ok")
    )
    return out


# ---- price ----------------------------------------------------------------------------------


def _catalogue_ids() -> set[tuple[str, str]]:
    return {(p.store.value, p.product_id) for p in load_seed_products(anchor=ANCHOR)}


def _price_rows(s: PriceSubject):
    return {(r.item_id, r.store): r for r in s.result.items}


def _price_expect(s: PriceSubject, params: dict[str, dict[str, Any]]) -> list[Outcome]:
    rows = _price_rows(s)
    by_name = {i.name: i for i in s.items}
    flags: dict[str, set[str]] = {}
    for f in s.result.flags:
        flags.setdefault(f.item_id, set()).add(f.kind)
    out: list[Outcome] = []
    for name, spec in params.items():
        item = by_name[name]
        for store in ACTIVE_STORES:
            sspec = spec.get(store.value)
            if not sspec:
                continue
            row = rows.get((item.item_id, store))
            tag = f"{name}@{store.value}"
            if row is None:
                out.append(_o("rows_present", False, f"{tag}: no row"))
                continue
            got = f"{row.product_id or 'none'} ({row.match_tier})"
            if "products" in sspec:
                out.append(
                    _o(
                        "product_allowed",
                        row.product_id in sspec["products"],
                        f"{tag}: {got}, allowed {sspec['products']}",
                    )
                )
            if "tiers" in sspec:
                out.append(
                    _o(
                        "tier",
                        row.match_tier in sspec["tiers"],
                        f"{tag}: {row.match_tier}, allowed {sspec['tiers']}",
                    )
                )
            if "on_special" in sspec:
                out.append(
                    _o(
                        "special",
                        row.on_special == sspec["on_special"],
                        f"{tag}: on_special={row.on_special}, want {sspec['on_special']}",
                    )
                )
            if "special_end" in sspec:
                got_end = row.special_end_date.isoformat() if row.special_end_date else None
                out.append(
                    _o(
                        "special",
                        got_end == sspec["special_end"],
                        f"{tag}: special ends {got_end}, want {sspec['special_end']}",
                    )
                )
            if "packs" in sspec:
                out.append(
                    _o(
                        "packs",
                        row.packs_needed == sspec["packs"],
                        f"{tag}: {row.packs_needed} packs, want {sspec['packs']}",
                    )
                )
            if sspec.get("best_value"):
                out.append(_best_value(s, item, store, row, sspec["products"], tag))
        if "pack_gap" in spec:
            has = "pack_gap" in flags.get(item.item_id, set())
            out.append(
                _o(
                    "pack_gap",
                    has == spec["pack_gap"],
                    f"{name}: pack_gap flag {has}, want {spec['pack_gap']}",
                )
            )
        if "flags" in spec:
            missing = sorted(set(spec["flags"]) - flags.get(item.item_id, set()))
            out.append(
                _o(
                    "flags",
                    not missing,
                    f"{name}: missing flags {missing}"
                    if missing
                    else f"{name}: {spec['flags']} present",
                )
            )
        if "no_flags" in spec:
            present = sorted(set(spec["no_flags"]) & flags.get(item.item_id, set()))
            out.append(
                _o(
                    "flags",
                    not present,
                    f"{name}: unexpected flags {present}"
                    if present
                    else f"{name}: none of {spec['no_flags']}",
                )
            )
    return out


def _best_value(s: PriceSubject, item, store: Store, row, products: list[str], tag: str) -> Outcome:
    """The pick costs the least of the acceptable products at this store."""
    cands = find_candidates(item, store, [item.name], s.source, s.config, ANCHOR)
    costs = {c.product.product_id: c.total_cost for c in cands if c.product.product_id in products}
    if not costs or row.total_cost is None:
        return _o("best_value", False, f"{tag}: no priced pick to compare")
    best = min(costs.values())
    return _o(
        "best_value",
        row.total_cost == best,
        f"{tag}: paid {row.total_cost}, cheapest acceptable {best}",
    )


def _price_implicit(s: PriceSubject, params: dict[str, Any]) -> list[Outcome]:
    known = _catalogue_ids()
    bad = [
        f"{r.item_id}@{r.store.value}:{r.product_id}"
        for r in s.result.items
        if r.product_id and (r.store.value, r.product_id) not in known
    ]
    want = len(s.items) * len(ACTIVE_STORES)
    return [
        _o("valid_product_ids", not bad, f"invented ids: {bad}" if bad else "all ids exist"),
        _o(
            "rows_complete", len(s.result.items) == want, f"{len(s.result.items)} rows, want {want}"
        ),
    ]


# ---- planner --------------------------------------------------------------------------------


def _pl_recommendation(s: PlannerSubject, params: Any) -> list[Outcome]:
    want = _as_list(params)
    return [
        _o(
            "recommendation",
            s.result.recommendation in want,
            f"{s.result.recommendation}, want {want}",
        )
    ]


def _pl_rule_verdict(s: PlannerSubject, params: Any) -> list[Outcome]:
    return [
        _o(
            "rule_verdict",
            s.facts.verdict.kind == params,
            f"code verdict {s.facts.verdict.kind}, want {params}",
        )
    ]


def _pl_store(s: PlannerSubject, params: Any) -> list[Outcome]:
    got = s.result.store.value if s.result.store else None
    return [_o("store", got == params, f"{got}, want {params}")]


def _pl_overridden(s: PlannerSubject, params: bool) -> list[Outcome]:
    return [
        _o(
            "overridden",
            s.result.overridden == params,
            f"overridden={s.result.overridden}, want {params}",
        )
    ]


def _pl_override_reason(s: PlannerSubject, params: Any) -> list[Outcome]:
    return [
        _o(
            "override_reason",
            s.result.override_reason == params,
            f"{s.result.override_reason}, want {params}",
        )
    ]


def _pl_confidence(s: PlannerSubject, params: Any) -> list[Outcome]:
    return [
        _o(
            "confidence",
            s.result.confidence in _as_list(params),
            f"{s.result.confidence}, want {params}",
        )
    ]


def _pl_flags_include(s: PlannerSubject, params: list[str]) -> list[Outcome]:
    have = {f.kind for f in s.result.flags}
    missing = sorted(set(params) - have)
    return [_o("flags", not missing, f"missing {missing}" if missing else f"{params} present")]


def _pl_flags_exclude(s: PlannerSubject, params: list[str]) -> list[Outcome]:
    present = sorted(set(params) & {f.kind for f in s.result.flags})
    return [_o("flags", not present, f"unexpected {present}" if present else f"none of {params}")]


def _pl_recheck(s: PlannerSubject, params: str) -> list[Outcome]:
    n = len(s.result.recheck_items)
    ok = n > 0 if params == "any" else n == 0
    return [_o("recheck", ok, f"{n} recheck items, want {params}")]


def _pl_tool_called(s: PlannerSubject, params: bool) -> list[Outcome]:
    called = bool(s.result.tool_calls)
    return [_o("tool_called", called == params, f"tool called={called}, want {params}")]


def _pl_split_assignment(s: PlannerSubject, params: dict[str, str]) -> list[Outcome]:
    ids = {r.item_name: r.item_id for r in s.rows}
    out = []
    for name, store in params.items():
        got = (s.result.split_assignment or {}).get(ids[name])
        out.append(
            _o(
                "split_assignment",
                got and got.value == store,
                f"{name}: {got.value if got else None}, want {store}",
            )
        )
    return out


def _pl_implicit(s: PlannerSubject, params: dict[str, Any]) -> list[Outcome]:
    facts, r = s.facts, s.result
    allowed = set(facts.amounts)
    tool = _what_if_tool(facts, allowed)  # re-running the tool collects every figure it returned
    for call in r.tool_calls:
        try:
            tool.func(**call.args)
        except (ValueError, TypeError):
            pass
    texts = [
        r.reasoning,
        *[t.text for t in r.tips],
        *[x.hint for x in r.recheck_items],
        *r.assumptions,
    ]
    invented = _invented_amounts(texts, allowed)
    out = [
        _o(
            "no_invented_amounts",
            not invented,
            f"invented {invented}" if invented else "every $ figure is a code number",
        )
    ]
    problems = [p for t in r.tips for p in _tip_problems(t, facts)]
    out.append(
        _o("tip_rules", not problems, "; ".join(problems) or f"{len(r.tips)} tips, all allowed")
    )
    offered = facts.split.savings >= facts.verdict.threshold
    out.append(
        _o(
            "tool_only_when_split_can_win",
            offered or not r.tool_calls,
            f"best split saves {facts.split.savings:.2f} vs threshold "
            f"{facts.verdict.threshold:.2f}; tool calls {len(r.tool_calls)}",
        )
    )
    return out


# ---- end to end -----------------------------------------------------------------------------


def _e2e_rec(s: E2ESubject, params: Any) -> list[Outcome]:
    got = (s.run.planner_output or {}).get("recommendation")
    return [_o("recommendation", got in _as_list(params), f"{got}, want {params}")]


def _e2e_min_items(s: E2ESubject, params: int) -> list[Outcome]:
    n = len((s.run.ingredient_output or {}).get("items", []))
    return [_o("min_items", n >= params, f"{n} items, want >= {params}")]


def _e2e_implicit(s: E2ESubject, params: dict[str, Any]) -> list[Outcome]:
    r = s.run
    return [
        _o(
            "completed",
            r.status == "completed",
            f"status {r.status}"
            + (f" ({r.failure_reason or r.stop_reason})" if r.status != "completed" else ""),
        ),
    ]


# ---- registry -------------------------------------------------------------------------------

CheckFn = Callable[[Any, Any], list[Outcome]]
CHECKS: dict[str, dict[str, CheckFn]] = {
    "ingredient": {
        "required_items": _ing_required_items,
        "quantity_ranges": _ing_ranges,
        "forbidden_avoid": _ing_forbidden_avoid,
        "forbidden_items": _ing_forbidden_items,
        "brands": _ing_brands,
        "merged": _ing_merged,
        "assumptions_mention": _ing_assumptions,
        "no_sanity_flags": _ing_no_flags,
        "min_items": _ing_min_items,
    },
    "price": {"expect": _price_expect},
    "planner": {
        "recommendation": _pl_recommendation,
        "rule_verdict": _pl_rule_verdict,
        "store": _pl_store,
        "overridden": _pl_overridden,
        "override_reason": _pl_override_reason,
        "confidence": _pl_confidence,
        "flags_include": _pl_flags_include,
        "flags_exclude": _pl_flags_exclude,
        "recheck": _pl_recheck,
        "tool_called": _pl_tool_called,
        "split_assignment": _pl_split_assignment,
    },
    "e2e": {"recommendation": _e2e_rec, "min_items": _e2e_min_items},
}
IMPLICIT = {
    "ingredient": _ing_implicit,
    "price": _price_implicit,
    "planner": _pl_implicit,
    "e2e": _e2e_implicit,
}
SHARED = {"max_calls"}  # accepted by every agent
IMPLICIT_NAMES: dict[str, list[str]] = {
    "ingredient": ["brand_only_if_named", "unique_lines", "pantry_staples_respected"],
    "price": ["valid_product_ids", "rows_complete"],
    "planner": ["no_invented_amounts", "tip_rules", "tool_only_when_split_can_win"],
    "e2e": ["completed", "within_call_budget"],
}


def check_names(agent: str) -> set[str]:
    return set(CHECKS[agent]) | SHARED


def evaluate(case, execution: Execution, meter: Meter) -> list[Outcome]:
    """All outcomes for a case: schema_valid, no_fallback, model_calls, then the case's checks."""
    s = execution.subject
    fell_back = meter.fell_back or bool(getattr(getattr(s, "run", None), "fell_back", False))
    out = [
        _o(
            "no_fallback",
            not fell_back,
            f"models {meter.models}" if fell_back else "primary model only",
        )
    ]
    limit = case.checks.get("max_calls")  # model calls; repairs and transport retries not counted
    if limit is not None:
        out.append(
            _o(
                "model_calls",
                meter.calls <= limit,
                f"{meter.calls} calls (+{meter.repairs} repairs, {meter.transport_retries} "
                f"transport retries), max {limit}",
            )
        )
    if s is None:
        out.append(_o("schema_valid", False, execution.error or "no output"))
        for name in case.checks:
            if name not in SHARED:
                out.append(_o(name, False, "not evaluated: the agent did not return a result"))
        for name in IMPLICIT_NAMES[case.agent]:
            out.append(_o(name, False, "not evaluated: the agent did not return a result"))
        return out
    out.insert(0, _o("schema_valid", True, "typed output validated"))
    params = dict(case.checks)
    for name, value in params.items():
        if name in SHARED:
            continue
        out.extend(CHECKS[case.agent][name](s, value))
    out.extend(IMPLICIT[case.agent](s, params))
    if case.agent == "e2e":  # the orchestrator reserves 9 calls; repairs count against it
        out.append(
            _o(
                "within_call_budget",
                meter.model_calls <= ESTIMATED_REQUESTS,
                f"{meter.model_calls} model calls, budget {ESTIMATED_REQUESTS}",
            )
        )
    return out
