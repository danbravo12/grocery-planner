"""Offline tests of the eval harness: the case files, the checks, the gate and the report.
No API calls. The planner cases' code-derived expectations are verified against build_facts."""

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from evals.avoid import violates
from evals.builders import build_rows
from evals.cases import AGENTS, Case, load_cases
from evals.checks import IMPLICIT_NAMES, check_names, evaluate
from evals.execute import ANCHOR, E2ESubject, IngredientSubject, PriceSubject, price_inputs
from evals.harness import EvalConfigError, Meter, prepare_environment
from evals.report import FLOORS, baseline_from, gate, render_markdown, summarize
from evals.run import run_case, validate_cases
from grocery_planner.agents.planner import PlannerOutput, build_facts
from grocery_planner.agents.price import PriceAgentResult
from grocery_planner.config import ACTIVE_STORES
from grocery_planner.db.dish_cache import InMemoryDishCache
from grocery_planner.domain import PlannerConfig
from grocery_planner.domain.request import PlanRequest
from grocery_planner.llm import FakeLLMClient
from grocery_planner.llm.types import SchemaValidationFailed, Usage
from grocery_planner.orchestrator.models import RunRecord
from grocery_planner.tools.price_lookup import SeededFilePriceSource, load_seed_products
from tests.helpers import COLES, WOOL, priced
from tests.test_ingredient_agent import batch, dish, ing

CASES = load_cases()
BY_ID = {c.id: c for c in CASES}


def of(agent: str) -> list[Case]:
    return [c for c in CASES if c.agent == agent]


# ---- the case files ---------------------------------------------------------------------------


def test_case_set_has_the_planned_shape():
    assert 30 <= len(CASES) <= 40
    counts = {a: len(of(a)) for a in AGENTS}
    assert counts == {"ingredient": 12, "price": 12, "planner": 10, "e2e": 3}
    assert all(c.notes.strip() for c in CASES)  # each case says why it exists


def test_checks_are_known_and_requests_are_capped():
    validate_cases(CASES)
    for c in CASES:
        assert isinstance(c.checks.get("max_requests"), int), c.id
        assert set(c.checks) <= check_names(c.agent), c.id


def test_requests_are_valid_plan_requests():
    for c in of("ingredient") + of("e2e"):
        raw = {k: v for k, v in c.input.items() if k != "parameters"}
        PlanRequest.model_validate(raw)  # lengths, ranges, the 20 dish cap
        PlannerConfig.model_validate(c.input.get("parameters", {}))


def test_price_expectations_name_real_products_and_items():
    catalogue = {(p.store.value, p.product_id) for p in load_seed_products(anchor=ANCHOR)}
    for c in of("price"):
        names = {i["name"] for i in c.input["items"]}
        assert set(c.checks["expect"]) <= names, c.id
        for spec in c.checks["expect"].values():
            for store in ("coles", "woolworths"):
                for pid in spec.get(store, {}).get("products", []):
                    assert (store, pid) in catalogue, f"{c.id}: {pid} is not a {store} product"
                for tier in spec.get(store, {}).get("tiers", []):
                    assert tier in {"exact", "equivalent", "substitute", "not_found"}
        price_inputs(c.input["items"])  # units and quantities parse


def test_planner_expectations_that_code_decides_hold_for_the_inputs():
    for c in of("planner"):
        rows, as_of = build_rows(c.input["build"])
        config = PlannerConfig.model_validate(c.input.get("config", {}))
        facts = build_facts(rows, ACTIVE_STORES, config, as_of)
        k = c.checks
        kinds = {f.kind for f in facts.flags}
        if "rule_verdict" in k:
            assert facts.verdict.kind == k["rule_verdict"], c.id
        if "confidence" in k:
            assert facts.confidence == k["confidence"], c.id
        assert set(k.get("flags_include", [])) <= kinds, c.id
        assert not set(k.get("flags_exclude", [])) & kinds, c.id
        if k.get("tool_called") is False:
            assert facts.split.savings < facts.verdict.threshold, c.id  # tool is not offered
        if k.get("overridden") is False:
            assert k["recommendation"] == facts.verdict.kind, c.id
        if k.get("override_reason") == "special_dependency":
            assert facts.special_dependent, c.id
        if k.get("override_reason") == "match_quality":
            assert facts.quality_issues, c.id
        if k.get("store"):
            assert facts.verdict.store.value == k["store"], c.id
        for name, store in k.get("split_assignment", {}).items():
            item_id = next(r.item_id for r in rows if r.item_name == name)
            assert facts.verdict.assignment[item_id].value == store, c.id


def test_the_split_case_is_the_only_one_that_offers_the_tool():
    offered = []
    for c in of("planner"):
        rows, as_of = build_rows(c.input["build"])
        facts = build_facts(rows, ACTIVE_STORES, PlannerConfig(), as_of)
        if facts.split.savings >= facts.verdict.threshold:
            offered.append(c.id)
    assert offered == ["pl-split-wins"]


# ---- avoid word lists -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("avoid", "name", "expected"),
    [
        ("dairy", "milk", True),
        ("dairy", "parmesan", True),
        ("dairy", "sour cream", True),
        ("dairy", "coconut milk", False),
        ("dairy", "peanut butter", False),
        ("dairy", "butternut pumpkin", False),
        ("dairy", "beef mince", False),
        ("nuts", "peanut butter", True),
        ("nuts", "cashews", True),
        ("nuts", "coconut milk", False),
        ("nuts", "nutmeg", False),
        ("gluten", "spaghetti", True),
        ("gluten", "soy sauce", True),
        ("gluten", "gluten-free pasta", False),
        ("gluten", "rice", False),
    ],
)
def test_avoid_matching(avoid, name, expected):
    assert violates(avoid, name) is expected


# ---- ingredient checks ------------------------------------------------------------------------


def ingredient_case(**checks) -> Case:
    return Case(
        id="t", agent="ingredient", notes="t",
        input={"meals": [{"meal_name": "M", "mains": ["x"], "people": 2, "days": 1,
                          "avoid": "dairy"}]},
        checks={"max_requests": 1, **checks},
    )  # fmt: skip


def run_ingredient(case: Case, *dishes) -> tuple[dict, Meter]:
    fake = FakeLLMClient([batch(*dishes)])
    r = run_case(case, lambda meter, hooks: _hooked(fake, meter), 1)
    return {o["name"]: o for o in r.outcomes}, r


def _hooked(fake: FakeLLMClient, meter: Meter) -> FakeLLMClient:
    fake.on_request = meter.on_request
    return fake


def test_ingredient_case_passes_and_fails_on_the_checks():
    case = ingredient_case(
        required_items=["beef mince", "potato"],
        quantity_ranges={"beef mince": [100, 200]},
        forbidden_avoid=["dairy"],
    )
    good = dish("d1", ing("beef mince", 150), ing("potato", 200))
    outcomes, result = run_ingredient(case, good)
    assert result.passed and result.metrics["requests"] == 1

    bad = dish("d1", ing("beef mince", 450), ing("milk", 100, "ml"))
    _, result = run_ingredient(case, bad)
    failed = {o["name"] for o in result.outcomes if not o["passed"]}
    assert failed == {"required_items", "quantity_range", "avoid_respected"}
    assert not result.passed


def test_ingredient_schema_failure_fails_every_check():
    case = ingredient_case(required_items=["potato"])
    fake = FakeLLMClient([SchemaValidationFailed(["{}"], ["dish_id: missing"])])
    r = run_case(case, lambda meter, hooks: _hooked(fake, meter), 1)
    assert not r.passed and r.error_kind == "schema"
    by = {o["name"]: o for o in r.outcomes}
    assert not by["schema_valid"]["passed"] and not by["required_items"]["passed"]
    assert set(IMPLICIT_NAMES["ingredient"]) <= set(by)


def test_brand_is_only_allowed_when_named():
    case = ingredient_case(brands={"tomato sauce": "Bellvue"})
    case.input["meals"][0]["mains"] = ["fries with Bellvue tomato sauce"]
    good = dish("d1", ing("tomato sauce", 30, "ml", brand="Bellvue"))
    _, r = run_ingredient(case, good)
    assert r.passed
    # a model that drops the named brand fails brand_when_named
    _, r = run_ingredient(case, dish("d1", ing("tomato sauce", 30, "ml")))
    assert [o["name"] for o in r.outcomes if not o["passed"]] == ["brand_when_named"]


# ---- price checks -----------------------------------------------------------------------------


def price_subject(case: Case, rows) -> PriceSubject:
    items = price_inputs(case.input["items"])
    result = PriceAgentResult(
        items=rows, usage=Usage(requests=1), rounds=1, downgrades=[], flags=[], reasons={}
    )
    return PriceSubject(items, result, SeededFilePriceSource(anchor=ANCHOR), PlannerConfig())


def test_price_checks_catch_a_wrong_product_and_a_worse_price():
    case = Case(
        id="t", agent="price", notes="t",
        input={"items": [{"name": "milk", "quantity": 2000, "unit": "ml"}]},
        checks={"max_requests": 1, "expect": {"milk": {
            "coles": {"products": ["col-milk-01", "col-milk-02"], "tiers": ["equivalent"],
                      "best_value": True},
            "woolworths": {"products": ["wol-milk-01", "wol-milk-02"], "best_value": True},
        }}},
    )  # fmt: skip
    from evals.execute import Execution

    good = [priced("i1", COLES, "milk", "col-milk-01"), priced("i1", WOOL, "milk", "wol-milk-01")]
    out = evaluate(case, Execution(price_subject(case, good)), Meter(requests=1))
    assert all(o.passed for o in out), [o for o in out if not o.passed]

    # branded milk costs more, so best_value fails; coconut milk is the wrong product
    worse = [
        priced("i1", COLES, "milk", "col-milk-02"),
        priced("i1", WOOL, "milk", "wol-coconut-milk-01"),
    ]
    out = evaluate(case, Execution(price_subject(case, worse)), Meter(requests=1))
    failed = {o.name for o in out if not o.passed}
    assert (
        failed == {"best_value", "product_allowed", "tier"}
        or {"best_value", "product_allowed"} <= failed
    )


def test_requests_check_uses_the_meter():
    from evals.execute import Execution

    case = Case(
        id="t", agent="price", notes="t",
        input={"items": [{"name": "milk", "quantity": 2000, "unit": "ml"}]},
        checks={"max_requests": 1, "expect": {}},
    )  # fmt: skip
    rows = [priced("i1", COLES, "milk", "col-milk-01"), priced("i1", WOOL, "milk", "wol-milk-01")]
    out = evaluate(case, Execution(price_subject(case, rows)), Meter(requests=3))
    assert [o.name for o in out if not o.passed] == ["requests"]


# ---- planner checks (through run_case with a scripted model) ----------------------------------


def planner_run(case_id: str, output: PlannerOutput):
    fake = FakeLLMClient([output])
    return run_case(BY_ID[case_id], lambda meter, hooks: _hooked(fake, meter), 1)


def test_planner_case_passes_with_the_right_answer():
    out = PlannerOutput(
        recommendation="no_meaningful_difference", overridden=False,
        reasoning="The two stores are within a dollar of each other.",
    )  # fmt: skip
    r = planner_run("pl-friday-dinner", out)
    assert r.passed, [o for o in r.outcomes if not o["passed"]]
    assert "no_invented_amounts" in {o["name"] for o in r.outcomes}


def test_planner_case_fails_when_the_store_is_wrong_or_amounts_are_invented():
    # An invented amount is rejected inside the agent's own validation in a real run, so here the
    # scripted output bypasses it; the eval check must still catch it independently.
    out = PlannerOutput(
        recommendation="no_meaningful_difference", overridden=False,
        reasoning="You would save $123.45 by shopping at Coles.",
    )  # fmt: skip
    r = planner_run("pl-friday-dinner", out)
    failed = {o["name"] for o in r.outcomes if not o["passed"]}
    assert failed == {"no_invented_amounts"}

    r = planner_run(
        "pl-large-gap-one-store",
        PlannerOutput(recommendation="no_meaningful_difference", overridden=True,
                      override_reason="match_quality", reasoning="Not sure."),
    )  # fmt: skip
    assert {"recommendation", "store", "overridden"} <= {
        o["name"] for o in r.outcomes if not o["passed"]
    }


# ---- end to end -------------------------------------------------------------------------------


def test_e2e_checks_completed_and_budget():
    now = datetime.now(UTC)
    run = RunRecord(id="r", status="completed", requests=12, created_at=now, updated_at=now,
                    planner_output={"recommendation": "one_store"},
                    ingredient_output={"items": [1, 2]})  # fmt: skip
    case = BY_ID["e2e-friday-dinner"]
    from evals.execute import Execution

    subject = E2ESubject(PlanRequest.model_validate({"meals": [case.input["meals"][0]]}), run, [])
    out = evaluate(case, Execution(subject), Meter(requests=12))
    failed = {o.name for o in out if not o.passed}
    assert failed == {"requests", "within_request_budget", "recommendation", "min_items"}


# ---- environment ------------------------------------------------------------------------------


def test_prepare_environment_needs_a_separate_eval_key(monkeypatch):
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("GEMINI_EVAL_API_KEY", "")
    with pytest.raises(EvalConfigError, match="GEMINI_EVAL_API_KEY is not set"):
        prepare_environment()
    monkeypatch.setenv("GEMINI_EVAL_API_KEY", "same-key")
    monkeypatch.setenv("GEMINI_API_KEY", "same-key")
    with pytest.raises(EvalConfigError, match="different key"):
        prepare_environment()
    monkeypatch.setenv("GEMINI_API_KEY", "demo-key")
    prepare_environment()
    import os

    assert os.environ["APP_ENV"] == "eval"


# ---- scoring, the gate and the report ---------------------------------------------------------


def fake_result(agent, case_id, passed, repeat=1, **metrics) -> dict:
    return {
        "id": case_id, "agent": agent, "repeat": repeat, "passed": passed, "error": None,
        "error_kind": None, "latency_s": 2.0,
        "outcomes": [{"name": "tier", "passed": passed, "detail": "x"},
                     {"name": "schema_valid", "passed": True, "detail": ""}],
        "metrics": {"requests": 1, "input_tokens": 100, "output_tokens": 10,
                    "would_cost_usd": "0.001", "fell_back": False, **metrics},
    }  # fmt: skip


def results(agent, passes, repeat=1):
    return [fake_result(agent, f"{agent}-{n}", ok, repeat) for n, ok in enumerate(passes)]


def test_summary_counts_cases_checks_and_repeats():
    rows = results("price", [True, True, False, True]) + results("price", [True] * 4, repeat=2)
    s = summarize(rows)["price"]
    assert (s["cases"], s["runs"], s["passed"]) == (4, 8, 7)
    assert s["repeat_rates"] == [0.75, 1.0] and s["rate_min"] == 0.75 and s["rate_mean"] == 0.875
    assert s["checks"]["tier"] == {"passed": 7, "total": 8, "rate": 0.875}
    assert s["requests"] == 8 and s["would_cost_usd"] == pytest.approx(0.008)


BASELINE = {"pass_rates": {"ingredient": 1.0, "price": 0.95, "planner": 1.0}}


def summary_with(**rates):
    rows = []
    for agent, rate in rates.items():
        n = 20
        rows += results(agent, [i < round(rate * n) for i in range(n)])
    return summarize(rows)


def test_gate_passes_within_ten_points_and_above_floors():
    assert gate(summary_with(ingredient=0.9, price=0.9, planner=0.9), BASELINE) == []


def test_gate_fails_on_more_than_ten_points_below_baseline():
    fails = gate(
        summary_with(ingredient=0.95, price=0.95, planner=0.95),
        {"pass_rates": {"planner": 1.0, "ingredient": 1.0}},
    )
    assert fails == []  # exactly 5 points
    fails = gate(summary_with(planner=0.9), {"pass_rates": {"planner": 1.0}})
    assert fails == []  # exactly 10 points is allowed (and above the 90% floor)
    fails = gate(summary_with(price=0.85), {"pass_rates": {"price": 0.99}})
    assert len(fails) == 1 and "points below the baseline" in fails[0]


def test_gate_fails_below_the_floors_even_without_a_baseline():
    fails = gate(summary_with(ingredient=0.8, price=0.8, planner=0.85), None)
    assert [f.split(":")[0] for f in fails] == ["ingredient", "price", "planner"]
    assert gate(summary_with(ingredient=0.85, price=0.85, planner=0.9), None) == []  # at the floor
    assert FLOORS == {"ingredient": 0.85, "price": 0.85, "planner": 0.90}


def test_gate_fails_if_any_run_fell_back():
    rows = results("planner", [True] * 10)
    rows[3]["metrics"]["fell_back"] = True
    fails = gate(summarize(rows), None)
    assert len(fails) == 1 and "fallback" in fails[0]


def test_gate_only_judges_agents_that_ran():
    assert gate(summarize(results("planner", [True] * 10)), BASELINE) == []


def test_baseline_is_written_as_a_draft():
    meta = {"timestamp": "20261015T000000Z", "model": "m", "repeat": 1}
    b = baseline_from(summary_with(planner=0.9), meta)
    assert b["status"] == "draft" and b["pass_rates"] == {"planner": 0.9}


def test_markdown_report_lists_scores_checks_usage_and_failures():
    rows = results("planner", [True, True, False, True]) + results("price", [True, True])
    meta = {"timestamp": "20261015T000000Z", "model": "m", "repeat": 1, "cases": 6,
            "as_of": "2026-10-12"}  # fmt: skip
    summary = summarize(rows)
    md = render_markdown(meta, summary, gate(summary, None), None, rows)
    assert "| planner | 4 | 3/4 | 75.0% |" in md and "## Pass rate per check" in md
    assert "FAIL: planner" in md and "**planner-2** failed 1 of 1" in md
    json.dumps(summary)  # the summary is plain JSON data
    assert "Decimal" not in md and isinstance(Decimal("1"), Decimal)


def test_ingredient_subject_dataclass_is_usable():
    request = PlanRequest.model_validate(
        {"meals": [{"meal_name": "M", "mains": ["x"], "people": 1, "days": 1}]}
    )
    from grocery_planner.agents.ingredient import run_ingredient_agent

    fake = FakeLLMClient([batch(dish("d1", ing("potato", 100)))])
    result = run_ingredient_agent(request, fake, InMemoryDishCache(), bypass_cache=True)
    assert IngredientSubject(request, result).result.items[0].name == "potato"
