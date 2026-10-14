import json
from decimal import Decimal as D

import pytest

from grocery_planner.agents.planner import (
    SYSTEM_PROMPT,
    Assign,
    PlannerOutput,
    Recheck,
    Tip,
    _what_if_tool,
    build_facts,
    check_output,
    expand_assignment,
    model_input,
    output_model,
    run_planner,
    summary_line,
)
from grocery_planner.config import ACTIVE_STORES
from grocery_planner.domain import PlannerConfig, Store
from grocery_planner.llm import (
    FakeLLMClient,
    FakeToolCalls,
    SchemaValidationFailed,
    ToolLoopExceeded,
)
from tests.fixtures import (
    doubtful_match_scenario,
    large_gap_scenario,
    load_price_fixture,
    special_tomorrow_scenario,
    split_scenario,
    with_total,
)
from tests.helpers import AS_OF, COLES, WOOL, not_found, priced, synthetic
from tests.test_llm import run as run_gemini
from tests.test_llm import text

CFG = PlannerConfig()
BOTH = list(ACTIVE_STORES)


def facts_of(fixture):
    return build_facts(fixture.items, BOTH, CFG, fixture.as_of)


def out(**over) -> PlannerOutput:
    base = dict(
        recommendation="no_meaningful_difference", overridden=False,
        reasoning="The two stores are within a dollar or two of each other.",
    )  # fmt: skip
    return PlannerOutput(**{**base, **over})


def plan(fixture, *script, allow_loopback=True):
    fake = FakeLLMClient(list(script))
    result = run_planner(fixture.items, fake, CFG, fixture.as_of, allow_loopback=allow_loopback)
    return result, fake


def item_id_named(fixture, name):
    return next(r.item_id for r in fixture.items if r.item_name == name)


# ---- the fixtures and scenarios --------------------------------------------------------------


def test_fixtures_load_without_an_api_call():
    for name in "abc":
        f = load_price_fixture(name)
        assert f.as_of.isoformat() == "2026-10-12" and f.captured_with_model.startswith("gemini")
        assert len(f.items) % 2 == 0 and f.items[0].store == COLES


def test_scenarios_have_the_intended_verdicts():
    for name in "abc":
        assert facts_of(load_price_fixture(name)).verdict.kind == "no_meaningful_difference"
    d = facts_of(large_gap_scenario()).verdict
    assert (d.kind, d.store) == ("one_store", WOOL) and d.gap > 2 * d.threshold
    e = facts_of(split_scenario()).verdict
    assert e.kind == "split" and e.cheapest_store == COLES
    assert e.gap < e.threshold <= e.split_savings == D(10)


# ---- facts, flags, confidence ---------------------------------------------------------------


def test_flags_for_the_edge_case_fixture():
    facts = facts_of(load_price_fixture("c"))
    kinds = {(f.kind, f.item_id) for f in facts.flags}
    sour = item_id_named(load_price_fixture("c"), "sourdough")
    assert ("missing", sour) in kinds  # sourdough is not sold at Coles
    assert {k for k, _ in kinds} >= {"missing", "pack_gap", "tier_gap"}
    pack = next(f for f in facts.flags if f.kind == "pack_gap")
    assert "coles 1000 g" in pack.message and "woolworths 1400 g" in pack.message
    assert facts.quality_issues and facts.confidence == "medium"


def rows(specs):
    """specs: item -> (coles_total, wool_total, coles_tier, wool_tier, coles_conf, wool_conf)."""
    out = []
    for item_id, (c, w, ct, wt, cc, wc) in specs.items():
        out += [synthetic(item_id, COLES, c, ct, cc), synthetic(item_id, WOOL, w, wt, wc)]
    return out


def simple(item_id, c="6", w="5", ct="equivalent", wt="equivalent", cc="high", wc="high"):
    return item_id, (c, w, ct, wt, cc, wc)


def confidence_of(items):
    return build_facts(items, BOTH, CFG, AS_OF).confidence


@pytest.mark.parametrize(
    "comparable, expected",
    [(10, "high"), (9, "high"), (8, "medium"), (7, "medium"), (6, "low"), (1, "low")],
)
def test_confidence_comparable_share_boundaries(comparable, expected):
    items = []
    for n in range(10):
        items += [synthetic(f"i{n}", COLES, "6")]
        items += [synthetic(f"i{n}", WOOL, "5", "equivalent" if n < comparable else "not_found")]
    assert confidence_of(items) == expected  # 90% and above can be high; below 70% is low


def test_confidence_low_when_a_top_saving_item_has_a_low_confidence_pick():
    savings = {"i1": "10", "i2": "9", "i3": "8", "i4": "7"}  # biggest first

    def build(low_item):
        specs = dict(
            simple(i, c=str(10 + int(v)), w="10", cc="low" if i == low_item else "high")
            for i, v in savings.items()
        )
        return rows(specs)

    assert confidence_of(build("i3")) == "low"  # third-biggest saving
    assert confidence_of(build("i4")) == "medium"  # fourth: not low, but not all high either
    assert confidence_of(build(None)) == "high"


def test_confidence_high_needs_gap_items_to_stay_within_half_the_savings():
    def build(gap_saving, plain_saving):
        specs = dict(
            [
                simple("gap", c=str(10 + gap_saving), w="10", ct="exact", wt="equivalent"),
                simple("plain", c=str(10 + plain_saving), w="10"),
            ]
        )
        return rows(specs)

    assert confidence_of(build(10, 5)) == "medium"  # tier-gap items are 2/3 of the savings
    assert confidence_of(build(5, 10)) == "high"  # 1/3
    assert confidence_of(build(5, 5)) == "high"  # exactly half is not "more than half"


def test_special_dependence_is_flagged_only_when_the_result_needs_it():
    def build(coles_chicken):
        chicken = [
            with_total(
                priced("chicken", COLES, "chicken breast", "col-chicken-breast-01"), coles_chicken
            ),
            with_total(priced("chicken", WOOL, "chicken breast", "wol-chicken-breast-01"), "20.00"),
        ]  # fmt: skip
        milk = [
            priced("milk", COLES, "milk", "col-milk-01"),
            priced("milk", WOOL, "milk", "wol-milk-01"),
        ]
        return build_facts(chicken + milk, BOTH, CFG, AS_OF)

    dependent = build("30.00")  # gap 10.10, of which the special accounts for 10.00
    assert dependent.verdict.kind == "one_store" and dependent.special_dependent == ["chicken"]
    flag = next(f for f in dependent.flags if f.kind == "special_dependency")
    assert (
        flag.store == WOOL and flag.ends_in_days == 3 and flag.ends_on.isoformat() == "2026-10-15"
    )
    assert "until 2026-10-15" in flag.message
    robust = build(
        "45.00"
    )  # gap 25.10: still wins without the special's 25.00? no: 25.10-25.00 < 8
    assert robust.special_dependent == ["chicken"]
    plus = build_facts(
        [*[with_total(r, r.total_cost + D(30)) if r.store == COLES else r
           for r in [priced("beef", COLES, "beef mince", "col-beef-mince-01"),
                     priced("beef", WOOL, "beef mince", "wol-beef-mince-01")]],
         *[with_total(priced("chicken", COLES, "chicken breast", "col-chicken-breast-01"), "14.00"),
           priced("chicken", WOOL, "chicken breast", "wol-chicken-breast-01")]],
        BOTH, CFG, AS_OF,
    )  # fmt: skip
    assert plus.verdict.kind == "one_store" and plus.special_dependent == []  # wins on beef anyway


def test_no_special_dependence_when_there_is_no_meaningful_difference():
    assert facts_of(load_price_fixture("c")).special_dependent == []


def test_summary_lines():
    a = facts_of(load_price_fixture("a"))
    assert summary_line(a, "no_meaningful_difference", None) == (
        "No meaningful difference: Woolworths is $1.00 cheaper on 5 common items "
        "(threshold $8.00).",
        D(0),
    )
    d = facts_of(large_gap_scenario())
    line, savings = summary_line(d, "one_store", None)
    assert (
        line
        == (
            f"Shop at Woolworths: ${d.verdict.gap:,.2f} cheaper than Coles on 7 common items "
            "(threshold $8.00)."
        )
        and savings == d.verdict.gap
    )
    e = facts_of(split_scenario())
    line, savings = summary_line(e, "split", e.split)
    assert line == (
        "Splitting the shop (1 of 7 common items bought away from Coles) saves $10.00 "
        "(threshold $8.00)."
    ) and savings == D(10)


# ---- model input -----------------------------------------------------------------------------


def test_model_input_is_json_with_code_numbers_and_categories():
    facts = facts_of(load_price_fixture("b"))
    data = json.loads(json.dumps(model_input(facts)))
    assert data["rule_verdict"]["kind"] == "no_meaningful_difference"
    assert data["rule_verdict"]["threshold"] == "8.00" and data["rule_verdict"]["gap"] == "1.00"
    assert (
        len(data["top_item_savings"]) == 7 and data["top_item_savings"][0]["name"] == "beef mince"
    )
    assert data["top_item_savings"][0]["saving"] == "2.40"
    assert data["items"]["i2"] == {"name": "beef mince", "category": "meat"}
    assert data["confidence"] == "high" and data["stores"] == ["coles", "woolworths"]
    assert data["coverage"]["comparable_items"] == 7 and "$" not in json.dumps(data)


def test_model_input_caps_top_items_at_ten():
    items = []
    for n in range(14):
        items += rows(dict([simple(f"i{n:02d}", c=str(10 + n), w="10")]))
    data = model_input(build_facts(items, BOTH, CFG, AS_OF))
    assert len(data["top_item_savings"]) == 10 and len(data["items"]) == 14


# ---- the agent with a scripted model ----------------------------------------------------------


def test_follows_the_verdict_in_one_request():
    f = load_price_fixture("a")
    result, fake = plan(f, out())
    assert result.recommendation == "no_meaningful_difference" and result.overridden is False
    assert result.summary_line.startswith("No meaningful difference: Woolworths is $1.00")
    assert (result.savings, result.confidence) == (D(0), "high")
    assert result.usage.requests == 1 and result.tool_calls == []
    assert result.rule_verdict.totals == {COLES: D("37.00"), WOOL: D("36.00")}
    assert (result.model, result.fell_back) == ("fake-model", False)
    call = fake.calls[0]
    assert call["system"] is SYSTEM_PROMPT and [t.name for t in call["tools"]] == ["what_if_split"]
    assert json.loads(call["user"])["rule_verdict"]["kind"] == "no_meaningful_difference"


def test_tool_use_then_answer_costs_two_requests():
    f = split_scenario()
    mince = item_id_named(f, "beef mince")
    ask = [[("what_if_split", {"assignments": [{"key": mince, "store": "woolworths"}]})]]
    answer = out(
        recommendation="split", overridden=False,
        split_assignment=[Assign(key=mince, store="woolworths")],
        reasoning="Buying the beef mince at Woolworths saves $10.00, clearing the $8.00 threshold.",
    )  # fmt: skip
    result, _ = plan(f, FakeToolCalls(ask, answer))
    assert result.usage.requests == 2 and len(result.tool_calls) == 1
    rec = result.tool_calls[0]
    assert rec.name == "what_if_split" and rec.error is None
    assert '"meets_threshold": true' in rec.result_summary or "true" in rec.result_summary
    assert result.recommendation == "split" and result.savings == D(10)
    assert result.split_assignment[mince] == WOOL and result.store is None
    assert result.summary_line.startswith("Splitting the shop (1 of 7 common items")


def test_never_more_than_two_tool_rounds():
    f = split_scenario()
    ask = [("what_if_split", {"assignments": [{"key": "meat", "store": "woolworths"}]})]
    with pytest.raises(ToolLoopExceeded):
        plan(f, FakeToolCalls([ask, ask, ask], out()))
    result, _ = plan(f, FakeToolCalls([ask, ask], out(recommendation="split", overridden=False)))
    assert result.usage.requests == 3  # two tool rounds plus the answer


def test_tool_errors_go_back_to_the_model():
    f = load_price_fixture("b")
    bad = [("what_if_split", {"assignments": [{"key": "dairy-ish", "store": "woolworths"}]})]
    worse = [("what_if_split", {"assignments": [{"key": "meat", "store": "aldi"}]})]
    result, _ = plan(f, FakeToolCalls([bad, worse], out()))
    assert [bool(t.error) for t in result.tool_calls] == [True, True]
    assert "unknown key 'dairy-ish'" in result.tool_calls[0].error
    assert "unknown store 'aldi'" in result.tool_calls[1].error


def test_override_is_recorded_and_store_comes_from_code():
    f = load_price_fixture("c")  # verdict: no meaningful difference; doubtful matches exist
    answer = out(
        recommendation="one_store", store="woolworths", overridden=False,  # wrong on both counts
        override_reason="match_quality",
        reasoning="Coles looks cheaper, but several matches are doubtful.",
    )  # fmt: skip
    result, _ = plan(f, answer)
    assert result.overridden is True and result.override_reason == "match_quality"
    assert result.recommendation == "one_store" and result.store == COLES  # the cheapest store
    assert result.summary_line.startswith("Shop at Coles: $3.30 cheaper than Woolworths")


def test_recheck_items_are_dropped_without_loopback():
    f = load_price_fixture("c")
    bacon = item_id_named(f, "bacon")
    answer = out(recheck_items=[Recheck(item_id=bacon, hint="try house brand bacon")])
    kept, _ = plan(f, answer)
    assert [r.item_id for r in kept.recheck_items] == [bacon]
    dropped, _ = plan(f, answer, allow_loopback=False)
    assert dropped.recheck_items == []


def test_planner_needs_comparable_items():
    f = load_price_fixture("a")
    items = [r for r in f.items if r.store == COLES]
    with pytest.raises(ValueError):
        run_planner(items, FakeLLMClient([out()]), CFG, f.as_of)


# ---- output validation ------------------------------------------------------------------------


def problems(facts, output, tool_amounts=frozenset()):
    return check_output(output, facts, set(tool_amounts))


def test_following_the_verdict_has_no_problems():
    assert problems(facts_of(load_price_fixture("a")), out()) == []
    e = facts_of(split_scenario())
    assert problems(e, out(recommendation="split", overridden=False)) == []  # verdict's own split


def test_override_needs_a_reason_and_consistent_flags():
    a = facts_of(load_price_fixture("a"))
    p = problems(a, out(recommendation="one_store", store="woolworths", overridden=True))
    assert any("override_reason is null" in x for x in p)
    p = problems(a, out(overridden=True))
    assert any("overridden must be false" in x for x in p)
    p = problems(a, out(override_reason="match_quality"))
    assert any("must be null when you follow" in x for x in p)


def test_override_reasons_must_be_backed_by_the_flags():
    a = facts_of(load_price_fixture("a"))  # one tier-gap item (fries), no ending special
    sp = out(recommendation="one_store", store="woolworths", overridden=True,
             override_reason="special_dependency")  # fmt: skip
    assert any("special_dependency is not allowed" in x for x in problems(a, sp))
    b = facts_of(load_price_fixture("b"))  # nothing doubtful at all
    mq = out(recommendation="one_store", store="woolworths", overridden=True,
             override_reason="match_quality")  # fmt: skip
    assert any("match_quality is not allowed" in x for x in problems(b, mq))
    assert problems(a, mq) == []


def test_one_store_must_name_the_cheapest_store():
    d = facts_of(large_gap_scenario())
    assert problems(d, out(recommendation="one_store", store="woolworths", overridden=False)) == []
    wrong = problems(d, out(recommendation="one_store", store="coles", overridden=False))
    assert any("must name the cheapest store 'woolworths'" in x for x in wrong)
    missing = problems(d, out(recommendation="one_store", overridden=False))
    assert any("cheapest store" in x for x in missing)


def test_split_must_clear_the_threshold_by_the_code_recomputation():
    b = facts_of(load_price_fixture("b"))  # no meaningful difference: any split saves < $8
    meat = [Assign(key="meat", store="woolworths")]
    p = problems(b, out(recommendation="split", overridden=True, override_reason="match_quality",
                        split_assignment=meat))  # fmt: skip
    assert any("below the threshold $8.00" in x for x in p)
    assert any("needs split_assignment" in x for x in problems(
        b, out(recommendation="split", overridden=True, override_reason="match_quality")
    ))  # fmt: skip
    e = facts_of(split_scenario())
    mince = next(i for i, n in e.names.items() if n == "beef mince")
    good = out(recommendation="split", overridden=False,
               split_assignment=[Assign(key=mince, store="woolworths")])  # fmt: skip
    assert problems(e, good) == []
    by_category = out(recommendation="split", overridden=False,
                      split_assignment=[Assign(key="meat", store="woolworths")])  # fmt: skip
    assert problems(e, by_category) == []  # mince is the only meat item
    weak = out(recommendation="split", overridden=False,
               split_assignment=[Assign(key="produce", store="woolworths")])  # fmt: skip
    assert any("below the threshold" in x for x in problems(e, weak))
    bad_key = out(recommendation="split", overridden=False,
                  split_assignment=[Assign(key="nope", store="woolworths")])  # fmt: skip
    assert any("unknown key 'nope'" in x for x in problems(e, bad_key))


def test_dollar_amounts_must_come_from_code_or_a_tool():
    a = facts_of(load_price_fixture("a"))
    ok = out(reasoning="Woolworths is $1.00 cheaper against a threshold of $8, with fillet $21.50.")
    assert problems(a, ok) == []
    bad = out(reasoning="Woolworths saves about $4.75 overall, or $1,250.00 a year.")
    p = problems(a, bad)
    assert len(p) == 1 and "$4.75" in p[0] and "$1,250.00" in p[0]
    in_tip = out(tips=[Tip(item_id="i2", text="Fillet could save $9.99 elsewhere.")])
    assert any("$9.99" in x for x in problems(a, in_tip))
    in_hint = out(recheck_items=[Recheck(item_id="i1", hint="Look for anything under $0.77")])
    assert any("$0.77" in x for x in problems(a, in_hint))
    from_tool = out(reasoning="Testing that split showed savings of $7.77.")
    assert problems(a, from_tool, {D("7.77")}) == []


def test_tip_and_recheck_item_ids_must_exist_and_rechecks_need_a_reason():
    c = facts_of(load_price_fixture("c"))
    fix = load_price_fixture("c")
    bacon, milk = item_id_named(fix, "bacon"), item_id_named(fix, "milk")
    assert any(
        "'zzz' does not exist" in x for x in problems(c, out(tips=[Tip(item_id="zzz", text="x")]))
    )
    assert any("'zzz' does not exist" in x for x in problems(
        c, out(recheck_items=[Recheck(item_id="zzz", hint="x")])))  # fmt: skip
    yoghurt = item_id_named(fix, "yoghurt")
    tip = Tip(item_id=yoghurt, text="Yoghurt costs less at Coles.")
    assert problems(c, out(tips=[tip])) == []
    assert problems(c, out(recheck_items=[Recheck(item_id=bacon, hint="same tier please")])) == []
    p = problems(c, out(recheck_items=[Recheck(item_id=milk, hint="check milk")]))
    assert any("not a low-confidence, tier-gap or substitute" in x for x in p)
    sourdough = item_id_named(fix, "sourdough")  # missing at Coles
    assert problems(c, out(recheck_items=[Recheck(item_id=sourdough, hint="any sourdough")])) == []


def test_list_length_caps():
    with pytest.raises(ValueError):
        out(tips=[Tip(item_id="i1", text="x")] * 4)
    with pytest.raises(ValueError):
        out(recheck_items=[Recheck(item_id="i1", hint="x")] * 6)


def test_output_model_raises_a_validation_error_with_the_problems():
    a = facts_of(load_price_fixture("a"))
    Checked = output_model(a, set())
    with pytest.raises(ValueError, match=r"\$4.75"):
        Checked.model_validate(out(reasoning="Saves $4.75.").model_dump())
    Checked.model_validate(out().model_dump())


# ---- through the real client (schema repair) --------------------------------------------------


def test_invented_amount_triggers_the_one_repair():
    f = load_price_fixture("a")
    bad = out(reasoning="Woolworths is cheaper by $4.75 across the basket.").model_dump_json()
    good = out().model_dump_json()
    h = run_gemini([text(bad), text(good)])
    result = run_planner(f.items, h.client, CFG, f.as_of)
    assert result.usage.requests == 2 and result.reasoning == out().reasoning
    repair = h.sent[1]["contents"][-1].parts[0].text
    assert "$4.75" in repair and "not in the input or a tool result" in repair
    cfg = h.sent[0]["config"]
    decl = cfg.tools[0].function_declarations[0]
    assert decl.name == "what_if_split" and cfg.automatic_function_calling.disable is True
    schema = decl.parameters_json_schema
    assert schema["properties"]["assignments"]["type"] == "array"  # a list of pairs, not a map
    assert "additionalProperties" not in json.dumps(schema)
    assert h.sent[1]["config"].tools is None  # the repair request carries no tools


def test_two_bad_answers_raise_schema_validation_failed():
    f = load_price_fixture("a")
    bad = out(overridden=True).model_dump_json()
    h = run_gemini([text(bad), text(bad)])
    with pytest.raises(SchemaValidationFailed):
        run_planner(f.items, h.client, CFG, f.as_of)


def test_override_without_reason_is_repaired():
    f = load_price_fixture("c")
    bad = out(recommendation="one_store", store="coles", overridden=True).model_dump_json()
    good = out().model_dump_json()
    h = run_gemini([text(bad), text(good)])
    result = run_planner(f.items, h.client, CFG, f.as_of)
    assert result.recommendation == "no_meaningful_difference" and result.usage.requests == 2
    assert "override_reason is null" in h.sent[1]["contents"][-1].parts[0].text


# ---- assignments and the tool ----------------------------------------------------------------


def test_expand_assignment_categories_items_and_precedence():
    facts = facts_of(split_scenario())
    ids = {n: i for i, n in facts.names.items()}
    full = expand_assignment(facts, [Assign(key="meat", store="Woolworths ")])
    assert full == {ids["beef mince"]: WOOL}
    mixed = expand_assignment(
        facts,
        [Assign(key="pantry", store="woolworths"), Assign(key=ids["kidney beans"], store="coles")],
    )
    assert mixed[ids["kidney beans"]] == COLES and mixed[ids["spaghetti"]] == WOOL
    with pytest.raises(ValueError, match="unknown store"):
        expand_assignment(facts, [Assign(key="meat", store="aldi")])
    with pytest.raises(ValueError, match="unknown key"):
        expand_assignment(facts, [Assign(key="bogus", store="coles")])


def test_what_if_tool_returns_code_numbers_and_registers_them_as_quotable():
    facts = facts_of(split_scenario())
    mince = next(i for i, n in facts.names.items() if n == "beef mince")
    seen: set[D] = set()
    result = _what_if_tool(facts, seen).func(assignments=[{"key": mince, "store": "woolworths"}])
    assert (
        result["savings_vs_cheapest_single_store"] == "$10.00" and result["meets_threshold"] is True
    )
    assert result["cheapest_single_store"] == "coles" and result["threshold"] == "$8.00"
    assert result["items_moved_from_cheapest_single_store"] == [
        {"item_id": mince, "name": "beef mince", "store": "woolworths"}
    ]
    assert D("10.00") in seen and D("87.10") in seen


def test_system_prompt_states_the_ground_rules():
    for phrase in ("DATA", "Follow rule_verdict", "special_dependency", "match_quality",
                   "what_if_split", "Never compute", "at most twice"):  # fmt: skip
        assert phrase in SYSTEM_PROMPT


def test_synthetic_not_found_helper_still_unused_here():
    assert not_found("x", COLES).total_cost is None and Store.ALDI not in BOTH


# ---- follow-up rules: tips, specials, assumptions, rechecks ------------------------------------


def test_tips_need_a_store_difference_of_a_quarter_of_the_threshold():
    fix = load_price_fixture("c")
    c = facts_of(fix)
    assert c.tip_min == D("2.00")
    milk, yoghurt, chicken = (item_id_named(fix, n) for n in ("milk", "yoghurt", "chicken breast"))
    small = problems(
        c, out(tips=[Tip(item_id=milk, text="Milk is a little cheaper at Woolworths.")])
    )
    assert len(small) == 1 and "at least $2.00" in small[0]  # milk differs by $0.10
    assert problems(c, out(tips=[Tip(item_id=yoghurt, text="Yoghurt costs less at Coles.")])) == []
    assert problems(c, out(tips=[])) == []  # empty is allowed
    # the minimum scales with the threshold: $20 threshold -> $5.00
    big = build_facts(fix.items, BOTH, PlannerConfig(savings_threshold_dollars=20), fix.as_of)
    assert big.tip_min == D("5.00")
    tip = Tip(item_id=chicken, text="Chicken costs less at Woolworths.")
    assert any("at least $5.00" in x for x in problems(big, out(tips=[tip])))
    assert json.loads(json.dumps(model_input(c)))["tip_minimum_saving"] == "2.00"


def test_a_tip_that_mentions_a_special_must_cite_the_end_date():
    fix = load_price_fixture("c")
    c = facts_of(fix)
    chicken, yoghurt = item_id_named(fix, "chicken breast"), item_id_named(fix, "yoghurt")
    bare = out(tips=[Tip(item_id=chicken, text="Chicken breast is on special at Woolworths.")])
    assert any(
        "must state when it ends, written exactly as 2026-10-15" in x for x in problems(c, bare)
    )
    dated = out(tips=[Tip(item_id=chicken, text="Chicken breast is on special until 2026-10-15.")])
    assert problems(c, dated) == []
    no_special = out(tips=[Tip(item_id=yoghurt, text="Yoghurt is on special at Coles.")])
    assert any("mentions a special, but that item has none" in x for x in problems(c, no_special))
    plain = out(tips=[Tip(item_id=chicken, text="Chicken breast is cheaper at Woolworths.")])
    assert problems(c, plain) == []  # not mentioning a special needs no date


def test_special_ending_flags_are_informational_and_follow_as_of():
    fix = load_price_fixture("c")

    def ending(as_of):
        facts = build_facts(fix.items, BOTH, CFG, as_of)
        return {(f.item_id, f.store.value): (f.ends_on.isoformat(), f.ends_in_days)
                for f in facts.flags if f.kind == "special_ending"}  # fmt: skip

    chicken, bacon = item_id_named(fix, "chicken breast"), item_id_named(fix, "bacon")
    assert ending(fix.as_of) == {
        (chicken, "woolworths"): ("2026-10-15", 3),
        (bacon, "coles"): ("2026-10-18", 6),
    }
    from datetime import date

    assert (chicken, "woolworths") not in ending(date(2026, 10, 7))  # 8 days away: outside 7
    assert ending(date(2026, 10, 8))[(chicken, "woolworths")] == ("2026-10-15", 7)  # exactly 7
    assert (chicken, "woolworths") not in ending(date(2026, 10, 16))  # already ended
    flag = next(
        f for f in facts_of(fix).flags if f.kind == "special_ending" and f.item_id == chicken
    )
    assert (
        flag.message
        == "chicken breast is on special at woolworths until 2026-10-15 (3 days from now)"
    )
    # information only: no override is allowed on a special_ending flag alone
    sp = out(recommendation="one_store", store="coles", overridden=True,
             override_reason="special_dependency")  # fmt: skip
    assert any("special_dependency is not allowed" in x for x in problems(facts_of(fix), sp))


def test_assumptions_may_only_cover_data_and_pricing():
    a = facts_of(load_price_fixture("a"))
    ok = out(assumptions=["Prices are as listed.", "Items without a match were left out."])
    assert problems(a, ok) == []
    for bad in (
        "I assumed you prefer the convenience of one store.",
        "This assumes you are able to visit both stores.",
        "The shopper wants the lowest price.",
        "Assumes your time is limited.",
    ):
        p = problems(a, out(assumptions=[bad]))
        assert len(p) == 1 and "may only cover data and pricing" in p[0], bad


def test_pack_gap_alone_does_not_justify_a_recheck():
    fix = load_price_fixture("c")
    c = facts_of(fix)
    yoghurt, bacon, sourdough = (item_id_named(fix, n) for n in ("yoghurt", "bacon", "sourdough"))
    assert yoghurt in c.quality_issues and yoghurt not in c.recheck_candidates  # pack gap only
    assert bacon in c.recheck_candidates and sourdough in c.recheck_candidates
    p = problems(c, out(recheck_items=[Recheck(item_id=yoghurt, hint="same pack size")]))
    assert any("not a low-confidence, tier-gap or substitute" in x for x in p)
    assert "pack-gap" not in SYSTEM_PROMPT and "pack gap" in SYSTEM_PROMPT  # still a flag kind
    recheck_line = next(
        line for line in SYSTEM_PROMPT.split("\n- ") if line.startswith("recheck_items")
    )
    assert "pack" not in recheck_line


def test_scenario_f_special_tomorrow_depends_on_the_special():
    f = special_tomorrow_scenario()
    facts = facts_of(f)
    mince = item_id_named(f, "beef mince")
    assert facts.verdict.kind == "one_store" and facts.verdict.store == WOOL
    assert facts.verdict.gap == D("11.40") and facts.special_dependent == [mince]
    dep = next(x for x in facts.flags if x.kind == "special_dependency")
    assert (dep.item_id, dep.ends_in_days) == (mince, 1) and dep.ends_on.isoformat() == "2026-10-13"
    assert any(x.kind == "special_ending" and x.ends_in_days == 1 for x in facts.flags)
    follow = out(recommendation="no_meaningful_difference", overridden=True,
                 override_reason="special_dependency")  # fmt: skip
    assert problems(facts, follow) == []
    # code backstop: following the verdict anyway is sent back for repair
    ignored = problems(facts, out(recommendation="one_store", store="woolworths", overridden=False))
    assert any("depends on a special that ends soon" in x for x in ignored)
    # and nothing is demanded when the verdict does not lean on a special
    assert not any("special" in x for x in problems(facts_of(load_price_fixture("b")), out()))
    assert any("match_quality is not allowed" in x for x in problems(
        facts, out(overridden=True, override_reason="match_quality")))  # fmt: skip


def test_scenario_g_doubtful_match_drives_the_saving():
    f = doubtful_match_scenario()
    facts = facts_of(f)
    mince = item_id_named(f, "beef mince")
    assert facts.verdict.kind == "one_store" and facts.verdict.gap == D("11.40")
    assert mince in facts.quality_issues and mince in facts.recheck_candidates
    assert facts.confidence == "low" and facts.special_dependent == []
    assert {x.kind for x in facts.flags} >= {"tier_gap"}
    recheck = Recheck(item_id=mince, hint="find a like-for-like mince")
    override = out(
        recommendation="no_meaningful_difference",
        overridden=True,
        override_reason="match_quality",
        recheck_items=[recheck],
    )
    assert problems(facts, override) == []
    assert any("special_dependency is not allowed" in x for x in problems(
        facts, out(overridden=True, override_reason="special_dependency")))  # fmt: skip


def test_prompt_version_and_assumption_rule_in_prompt():
    result, _ = plan(load_price_fixture("a"), out())
    assert result.prompt_version == "2"
    assert "Never assume anything about the shopper" in SYSTEM_PROMPT
    assert "tip_minimum_saving" in SYSTEM_PROMPT and "state the date it ends" in SYSTEM_PROMPT
    assert "MUST recommend no_meaningful_difference" in SYSTEM_PROMPT
    assert "never tell the shopper to trust a result whose confidence is low" in SYSTEM_PROMPT


def test_summary_line_says_why_an_override_held_the_verdict_back():
    f = facts_of(special_tomorrow_scenario())
    line, savings = summary_line(f, "no_meaningful_difference", None, "special_dependency")
    assert line == (
        "No meaningful difference: Woolworths is $11.40 cheaper on 7 common items "
        "(threshold $8.00), but the saving depends on a special ending 2026-10-13."
    ) and savings == D(0)
    g = facts_of(doubtful_match_scenario())
    line, _ = summary_line(g, "no_meaningful_difference", None, "match_quality")
    assert line.endswith("(threshold $8.00), but the comparison is not like for like.")
    assert summary_line(g, "one_store", None)[0].endswith("(threshold $8.00).")
