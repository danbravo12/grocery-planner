"""Live runs of the planner agent (real Gemini) on saved price fixtures. Run with:
    uv run pytest -m live -s tests/test_planner_live.py
Uses one request per scenario, plus one per tool round or repair."""

import time

import pytest

from grocery_planner.agents.planner import run_planner
from grocery_planner.config import ACTIVE_STORES
from grocery_planner.domain import PlannerConfig
from grocery_planner.env import getenv
from grocery_planner.llm import GeminiClient
from tests.fixtures import (
    doubtful_match_scenario,
    large_gap_scenario,
    load_price_fixture,
    special_tomorrow_scenario,
    split_scenario,
)

pytestmark = pytest.mark.live
CFG = PlannerConfig()


@pytest.fixture(scope="module")
def llm():
    if not getenv("GEMINI_API_KEY"):
        pytest.skip("GEMINI_API_KEY not set")
    return GeminiClient()


def go(label, fixture, llm):
    start = time.perf_counter()
    r = run_planner(fixture.items, llm, CFG, fixture.as_of, ACTIVE_STORES)
    seconds = time.perf_counter() - start
    v, u = r.rule_verdict, r.usage
    print(f"\n=== {label} ===")
    totals = {s.value: f"{t:.2f}" for s, t in v.totals.items()}
    print(
        f"rule_verdict: {v.kind} (store={v.store.value if v.store else None}) totals={totals} "
        f"gap={v.gap:.2f} threshold={v.threshold:.2f} split_savings={v.split_savings:.2f}"
    )
    print(f"recommendation: {r.recommendation}  store={r.store.value if r.store else None}"
          f"  overridden={r.overridden}  override_reason={r.override_reason}")  # fmt: skip
    if r.split_assignment:
        print("split_assignment:", {k: s.value for k, s in r.split_assignment.items()})
    print(f"summary_line: {r.summary_line}")
    print(f"savings: {r.savings:.2f}  confidence: {r.confidence}")
    print(f"reasoning: {r.reasoning}")
    print("tips:", [(t.item_id, t.text) for t in r.tips])
    print("recheck_items:", [(x.item_id, x.hint) for x in r.recheck_items])
    print("assumptions:", r.assumptions)
    print("flags:", [(f.kind, f.item_id, f.message) for f in r.flags])
    print("tool_calls:", [(t.name, t.args, t.result_summary, t.error) for t in r.tool_calls])
    print(f"requests={u.requests} input_tokens={u.input_tokens} output_tokens={u.output_tokens} "
          f"thinking_tokens={u.thinking_tokens} latency={seconds:.1f}s model={r.model} "
          f"fell_back={r.fell_back}")  # fmt: skip
    return r


def test_a(llm):
    r = go("(a) Friday dinner", load_price_fixture("a"), llm)
    assert r.recommendation == "no_meaningful_difference" and not r.overridden


def test_b(llm):
    r = go("(b) bolognese and chilli", load_price_fixture("b"), llm)
    assert r.recommendation == "no_meaningful_difference" and not r.overridden


def test_c(llm):
    go("(c) seeded edge cases", load_price_fixture("c"), llm)


def test_d_large_gap(llm):
    r = go("(d) large gap: Coles 20% dearer", large_gap_scenario(), llm)
    assert r.recommendation == "one_store" and r.store.value == "woolworths"


def test_e_split(llm):
    r = go("(e) beef mince $10 cheaper at Woolworths", split_scenario(), llm)
    assert r.recommendation == "split"


def test_f_saving_only_from_a_special_ending_tomorrow(llm):
    r = go("(f) beef mince special at Woolworths ends tomorrow", special_tomorrow_scenario(), llm)
    assert r.rule_verdict.kind == "one_store"
    assert r.overridden is True and r.override_reason == "special_dependency"


def test_g_saving_only_from_a_doubtful_match(llm):
    r = go("(g) beef mince saving rests on a doubtful match", doubtful_match_scenario(), llm)
    assert r.rule_verdict.kind == "one_store" and r.confidence == "low"
