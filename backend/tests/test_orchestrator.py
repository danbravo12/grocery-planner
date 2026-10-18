import json
import threading
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D

from grocery_planner.agents.planner import PlannerOutput, Recheck
from grocery_planner.agents.price import ItemOut, MatchBatch, PickOut
from grocery_planner.cli import load_request, main, render
from grocery_planner.db.dish_cache import InMemoryDishCache
from grocery_planner.domain import PlannerConfig
from grocery_planner.domain.ingredients import (
    DishIngredient,
    ExpansionBatch,
    ModelDish,
)
from grocery_planner.domain.request import Meal, PlanRequest
from grocery_planner.llm import (
    FakeLLMClient,
    GeminiClient,
    LLMUnavailable,
    QuotaExhausted,
    SchemaValidationFailed,
)
from grocery_planner.orchestrator import (
    ESTIMATED_REQUESTS,
    InMemoryRunStore,
    NoopBudgetGuard,
    Runner,
    RunnerSettings,
    events_from_state,
    follow_events,
)
from grocery_planner.tools.price_lookup import SeededFilePriceSource
from tests.test_llm import FakeGenai, per_day, per_minute, text

AS_OF = date(2026, 10, 14)
SOURCE = SeededFilePriceSource(anchor=AS_OF)
COLES, WOOL = "coles", "woolworths"


# ---- scripted model outputs -----------------------------------------------------------------


def ing(name, qty, unit="g", in_vocab=True):
    return DishIngredient(
        name=name, quantity_per_serving=D(str(qty)), unit=unit, in_vocabulary=in_vocab
    )


def ingredients(*items) -> ExpansionBatch:
    return ExpansionBatch(dishes=[ModelDish(dish_id="d1", ingredients=list(items))])


def pick(store, cid, tier="equivalent"):
    if cid is None:
        return PickOut(
            store=store, candidate_id=None, tier="not_found", confidence="low", reason="none"
        )
    return PickOut(store=store, candidate_id=cid, tier=tier, confidence="high", reason="ok")


def prices(*entries) -> MatchBatch:
    """entries: (item_id, coles_candidate_or_None, woolworths_candidate_or_None)."""
    return MatchBatch(
        items=[ItemOut(item_id=i, picks=[pick(COLES, c), pick(WOOL, w)]) for i, c, w in entries]
    )


def planner(**over) -> PlannerOutput:
    base = dict(
        recommendation="no_meaningful_difference", overridden=False,
        reasoning="The two stores are within a dollar or so.",
    )  # fmt: skip
    return PlannerOutput(**{**base, **over})


REQUEST = PlanRequest(meals=[Meal(meal_name="Brekky", mains=["bacon and eggs"], people=2, days=1)])
# eggs (dairy) sorts before bacon (meat): i1 eggs 4 each, i2 bacon 200 g


def happy_script():
    return [
        ingredients(ing("bacon", 100), ing("eggs", 2, "each")),
        prices(("i1", "col-eggs-01", "wol-eggs-01"), ("i2", "col-bacon-02", "wol-bacon-01")),
        planner(),
    ]


def factory_for(script_fn, cls=FakeLLMClient):
    def make(hooks):
        return cls(script_fn(), on_request=hooks.on_request, before_request=hooks.before_request)

    return make


class Recorder:
    """A BudgetGuard that remembers the order of calls."""

    def __init__(self):
        self.calls = []

    def reserve(self, run_id, estimated_requests):
        self.calls.append(("reserve", run_id, estimated_requests))

    def record(self, run_id, model, requests):
        self.calls.append(("record", run_id, model, requests))

    def release(self, run_id):
        self.calls.append(("release", run_id))

    def before_request(self, model):
        pass

    def fallback_allowed(self):
        return True

    def quota_exhausted(self, model, kind):
        pass


def make_runner(script_fn=happy_script, request=REQUEST, store=None, source=SOURCE, **kw):
    store = store or InMemoryRunStore()
    kw.setdefault("llm_factory", factory_for(script_fn))
    runner = Runner(store, cache=InMemoryDishCache(), source=source, **kw)
    return runner, store


def run(runner, request=REQUEST, config=None, **kw):
    run_id = runner.create_run(request, config or PlannerConfig(), AS_OF, **kw)
    return run_id, runner.run_plan(run_id)


def step_names(store, run_id):
    return [(s.name, s.status) for s in store.get_steps(run_id)]


# ---- happy path and the run record -------------------------------------------------------------


def test_run_row_is_created_first_with_a_long_random_id_and_the_parameters_used():
    runner, store = make_runner()
    config = PlannerConfig(savings_threshold_dollars="10", stop_not_found_pct="0.30")
    run_id = runner.create_run(REQUEST, config, AS_OF, bypass_cache=True)
    row = store.get_run(run_id)
    assert (
        row.status == "queued"
        and len(run_id) == 36
        and run_id != runner.create_run(REQUEST, config, AS_OF)
    )
    assert row.input == REQUEST.model_dump(mode="json")
    assert row.estimated_requests == ESTIMATED_REQUESTS == 9
    p = row.parameters
    assert (
        p["config"]["savings_threshold_dollars"] == "10"
        and p["config"]["stop_not_found_pct"] == "0.30"
    )
    assert p["as_of"] == "2026-10-14" and p["bypass_cache"] is True and p["deadline_seconds"] == 180
    assert p["stores"] == [COLES, WOOL] and set(p["prompt_versions"]) == {
        "ingredient",
        "price",
        "planner",
    }
    assert store.get_steps(run_id) == []


def test_happy_path_stores_every_step_and_fills_the_run_totals():
    runner, store = make_runner()
    run_id, row = run(runner)
    assert row.status == "completed" and row.failure_reason is None and row.stop_reason is None
    assert step_names(store, run_id) == [
        ("ingredient", "completed"), ("price", "completed"), ("planner", "completed"),
    ]  # fmt: skip
    steps = store.get_steps(run_id)
    assert [s.seq for s in steps] == [1, 2, 3] and [s.agent for s in steps] == [
        "ingredient", "price", "planner",
    ]  # fmt: skip
    assert all(s.model == "fake-model" and s.requests == 1 and s.fell_back is False for s in steps)
    assert all(s.latency_ms is not None and s.finished_at and s.summary for s in steps)
    assert steps[0].summary == "2 items from 1 dishes (0 cached)"
    assert steps[2].summary.startswith("No meaningful difference")
    assert steps[1].input["items"][0]["item_id"] == "i1"
    assert row.requests == 3 and row.models_used == ["fake-model"] and row.fell_back is False
    assert row.loopback_used is False and row.completed_at and row.latency_ms is not None
    assert [i["name"] for i in row.ingredient_output["items"]] == ["eggs", "bacon"]
    assert row.price_output["rounds"] == 1 and len(row.price_output["items"]) == 4
    assert row.planner_output["recommendation"] == "no_meaningful_difference"
    totals = row.planner_output["rule_verdict"]["totals"]
    assert {k: D(v) for k, v in totals.items()} == {COLES: D("9.50"), WOOL: D("11.00")}


class OverrideSource:
    """The seed catalogue with a few prices changed, to build a basket where a split pays."""

    def __init__(self, prices: dict[str, str]):
        self._prices = {k: D(v) for k, v in prices.items()}

    def search(self, term, store, limit, brand=None):
        return [
            p.model_copy(update={"price": self._prices.get(p.product_id, p.price)})
            for p in SOURCE.search(term, store, limit, brand)
        ]


SPLIT_SOURCE = OverrideSource({"wol-eye-fillet-01": "12.00", "wol-bacon-01": "14.30"})
SPLIT_REQUEST = PlanRequest(
    meals=[Meal(meal_name="Dinner", mains=["steak and bacon"], people=2, days=1)]
)


def split_script():
    """Fillet is $10 cheaper at Woolworths, bacon $8.80 cheaper at Coles: Woolworths is cheaper
    overall by only $1.20 (no one-store win), but splitting saves $8.80, so the planner is
    offered its what-if tool."""
    from grocery_planner.llm import FakeToolCalls

    ask = [("what_if_split", {"assignments": [{"key": "meat", "store": "coles"}]})]
    return [
        ingredients(ing("beef eye fillet", 200), ing("bacon", 125)),
        prices(
            ("i1", "col-bacon-01", "wol-bacon-01"),
            ("i2", "col-eye-fillet-01", "wol-eye-fillet-01"),
        ),
        FakeToolCalls([ask], planner(recommendation="split", overridden=False)),
    ]  # fmt: skip


def test_planner_tool_calls_are_stored_on_the_step():
    runner, store = make_runner(split_script, source=SPLIT_SOURCE)
    run_id, row = run(runner, request=SPLIT_REQUEST)
    assert row.status == "completed"
    planner_step = store.get_steps(run_id)[2]
    assert planner_step.requests == 2 and planner_step.tool_calls[0]["name"] == "what_if_split"
    assert row.requests == 4 and row.planner_output["recommendation"] == "split"
    assert row.planner_output["rule_verdict"]["kind"] == "split"


def test_planner_gets_no_tool_when_no_split_can_reach_the_threshold():
    fakes = []

    def make(hooks):
        fakes.append(FakeLLMClient(happy_script(), on_request=hooks.on_request))
        return fakes[-1]

    runner, store = make_runner(llm_factory=make)
    run_id, row = run(runner)
    planner_call = fakes[0].calls[2]
    assert planner_call["tools"] is None and "what_if_split" not in planner_call["system"]
    assert store.get_steps(run_id)[2].requests == 1 and row.requests == 3


# ---- loop-back --------------------------------------------------------------------------------


def loopback_script(second_planner_rechecks=True):
    again = [Recheck(item_id="i2", hint="again")] if second_planner_rechecks else []
    return [
        ingredients(ing("bacon", 100), ing("eggs", 2, "each")),
        prices(("i1", "col-eggs-01", "wol-eggs-01"), ("i2", "col-bacon-02", "wol-bacon-01")),
        planner(recheck_items=[Recheck(item_id="i2", hint="find like-for-like bacon")]),
        prices(("i2", "col-bacon-01", "wol-bacon-01")),
        planner(recheck_items=again),
    ]


def test_loopback_runs_exactly_once_and_the_second_planner_cannot_loop_again():
    fakes = []

    def make(hooks):
        fakes.append(FakeLLMClient(loopback_script(), on_request=hooks.on_request))
        return fakes[-1]

    runner, store = make_runner(llm_factory=make)
    run_id, row = run(runner)
    assert row.status == "completed" and row.loopback_used is True
    assert step_names(store, run_id) == [
        ("ingredient", "completed"), ("price", "completed"), ("planner", "completed"),
        ("reprice", "completed"), ("planner_2", "completed"),
    ]  # fmt: skip
    assert fakes[0].script == []  # exactly five model calls, never a sixth
    steps = store.get_steps(run_id)
    assert [s.attempt for s in steps] == [1, 1, 1, 2, 2]
    assert steps[3].input["hints"] == {"i2": "find like-for-like bacon"}
    assert [i["item_id"] for i in steps[3].input["items"]] == ["i2"]
    assert steps[4].output["recheck_items"] == []  # planner_2 ran with allow_loopback=False
    assert row.planner_output["recheck_items"] == []
    assert row.requests == 5
    # the re-priced bacon (house brand at Coles) replaced the first pick in the stored result
    coles_bacon = next(
        r for r in row.price_output["items"] if r["item_id"] == "i2" and r["store"] == COLES
    )
    assert coles_bacon["product_id"] == "col-bacon-01"


def test_loopback_events_and_never_a_third_pass():
    runner, store = make_runner(script_fn=loopback_script)
    run_id, _ = run(runner)
    kinds = [(e.type, e.name) for e in runner.events(run_id)]
    assert kinds == [
        ("step_started", "ingredient"), ("step_completed", "ingredient"),
        ("step_started", "price"), ("step_completed", "price"),
        ("step_started", "planner"), ("step_completed", "planner"),
        ("loopback", None),
        ("step_started", "reprice"), ("step_completed", "reprice"),
        ("step_started", "planner_2"), ("step_completed", "planner_2"),
        ("run_completed", None),
    ]  # fmt: skip
    loop = next(e for e in runner.events(run_id) if e.type == "loopback")
    assert loop.data == {"item_ids": ["i2"]}


def test_merge_price_results_replaces_only_the_redone_items():
    runner, store = make_runner(script_fn=loopback_script)
    run_id, row = run(runner)
    from grocery_planner.agents.price import PriceAgentResult

    final = PriceAgentResult.model_validate(row.price_output)
    assert [r.item_id for r in final.items] == ["i1", "i1", "i2", "i2"]  # order kept
    assert final.rounds == 2 and final.usage.requests == 2


# ---- stop -------------------------------------------------------------------------------------


def stop_script():
    return [
        ingredients(
            ing("bacon", 100),
            ing("durian", 100, in_vocab=False),
            ing("dragon fruit", 100, in_vocab=False),
            ing("lychee", 100, in_vocab=False),
        ),
        prices(
            ("i1", "col-bacon-01", "wol-bacon-01"),
            ("i2", None, None),
            ("i3", None, None),
            ("i4", None, None),
        ),
    ]


def test_stop_on_too_many_missing_items_keeps_the_ingredient_list_and_skips_the_planner():
    runner, store = make_runner(script_fn=stop_script)
    run_id, row = run(runner)
    assert row.status == "stopped" and row.stop_reason == "too_many_unmatched"
    assert row.failure_reason is None and row.completed_at
    assert step_names(store, run_id) == [
        ("ingredient", "completed"), ("price", "completed"), ("stop", "completed"),
    ]  # fmt: skip
    assert row.planner_output is None
    assert len(row.ingredient_output["items"]) == 4 and row.price_output is not None
    assert "3 of 4 items have no usable match at any store" in row.user_message
    assert "Your ingredient list is below" in row.user_message
    details = store.get_steps(run_id)[2].output
    assert [f["name"] for f in details["failed_items"]] == ["dragon fruit", "durian", "lychee"]
    assert all(f["found_nowhere"] for f in details["failed_items"])
    assert details["failed_items"][0]["stores"][COLES] == {
        "match_tier": "not_found", "product": None, "reason": "none",
    }  # fmt: skip
    events = runner.events(run_id)
    assert events[-1].type == "run_failed" and events[-1].data["status"] == "stopped"
    assert (
        events[-1].data["reason"] == "too_many_unmatched" and events[-1].summary == row.user_message
    )
    assert row.requests == 2


def test_stop_when_the_stores_share_no_comparable_items():
    def script():
        return [
            ingredients(ing("sourdough", 100), ing("potato gems", 100)),
            prices(("i1", None, "wol-sourdough-01"), ("i2", "col-potato-gems-01", None)),
        ]

    runner, store = make_runner(script)
    _, row = run(runner)
    assert row.status == "stopped" and row.stop_reason == "not_comparable"
    assert "no comparable items" in row.user_message


def test_stop_thresholds_come_from_the_run_config():
    # 1 of 4 items found nowhere is 25%: exactly at the default threshold, so no stop...
    def script():
        return [
            ingredients(ing("bacon", 100), ing("eggs", 2, "each"), ing("milk", 100, "ml"),
                        ing("durian", 10, in_vocab=False)),
            prices(("i1", "col-eggs-01", "wol-eggs-01"), ("i2", "col-milk-01", "wol-milk-01"),
                   ("i3", "col-bacon-01", "wol-bacon-01"), ("i4", None, None)),
            planner(),
        ]  # fmt: skip

    runner, _ = make_runner(script)
    assert run(runner)[1].status == "completed"
    # ...and a stricter run parameter (10%) stops the same list
    runner, _ = make_runner(script)
    _, row = run(runner, config=PlannerConfig(stop_not_found_pct="0.10"))
    assert row.status == "stopped" and row.stop_reason == "too_many_unmatched"


def test_nothing_to_price_stops_before_the_price_agent():
    def script():
        return [ingredients(ing("salt", 2))]  # a pantry staple, dropped by default

    runner, store = make_runner(script)
    _, row = run(runner)
    assert row.status == "stopped" and row.stop_reason == "no_items"
    assert "pantry staples" in row.user_message
    assert [n for n, _ in step_names(store, row.id)] == ["ingredient", "stop"]


# ---- failures ---------------------------------------------------------------------------------


def failing_script(at, error):
    def script():
        s = happy_script()
        s[at] = error
        return s

    return script


def test_schema_failure_in_the_planner_fails_the_run_but_keeps_everything_before_it():
    runner, store = make_runner(failing_script(2, SchemaValidationFailed(["a", "b"], ["x"])))
    run_id, row = run(runner)
    assert row.status == "failed" and row.failure_reason == "schema_validation"
    assert step_names(store, run_id) == [
        ("ingredient", "completed"), ("price", "completed"), ("planner", "failed"),
    ]  # fmt: skip
    assert store.get_steps(run_id)[2].error == "SchemaValidationFailed"
    assert row.ingredient_output is not None and row.price_output is not None
    assert row.planner_output is None
    assert "ingredient list is below" in row.user_message and row.completed_at
    assert row.requests == 2  # only the completed steps' requests are known
    assert runner.events(run_id)[-1].type == "run_failed"


def test_model_unavailable_fails_the_run_and_keeps_the_ingredient_list():
    runner, store = make_runner(failing_script(1, LLMUnavailable("down")))
    run_id, row = run(runner)
    assert row.status == "failed" and row.failure_reason == "model_unavailable"
    assert row.ingredient_output is not None and row.price_output is None
    assert "unavailable" in row.user_message


def test_failure_before_any_output_has_no_ingredient_list_to_offer():
    runner, _ = make_runner(failing_script(0, LLMUnavailable("down")))
    _, row = run(runner)
    assert row.status == "failed" and row.ingredient_output is None
    assert "ingredient list" not in row.user_message


def test_failure_in_the_loopback_pass_keeps_the_first_planner_result():
    def script():
        s = loopback_script()
        s[3] = LLMUnavailable("down")
        return s

    runner, store = make_runner(script)
    run_id, row = run(runner)
    assert row.status == "failed" and row.loopback_used is True
    assert [n for n, _ in step_names(store, run_id)] == [
        "ingredient",
        "price",
        "planner",
        "reprice",
    ]
    assert row.planner_output["recommendation"] == "no_meaningful_difference"


def gemini_factory(script, **kw):
    def make(hooks):
        return GeminiClient(
            genai_client=FakeGenai(list(script)), model="primary", fallback_model="backup",
            on_request=hooks.on_request, before_request=hooks.before_request,
            fallback_allowed=hooks.fallback_allowed, on_quota_exhausted=hooks.on_quota_exhausted,
            sleep=lambda s: None, **kw,
        )  # fmt: skip

    return make


def ingredient_json():
    return ingredients(ing("bacon", 100), ing("eggs", 2, "each")).model_dump_json()


def price_json():
    return prices(
        ("i1", "col-eggs-01", "wol-eggs-01"), ("i2", "col-bacon-01", "wol-bacon-01")
    ).model_dump_json()


def test_quota_exhausted_on_both_models_is_a_daily_quota_failure():
    script = [text(ingredient_json()), per_day(), per_day()]  # price step: primary and backup
    runner, store = make_runner(llm_factory=gemini_factory(script))
    run_id, row = run(runner)
    assert row.status == "failed" and row.failure_reason == "daily_quota"
    assert "today's AI quota" in row.user_message and "ingredient list is below" in row.user_message
    assert store.get_steps(run_id)[1].error == "QuotaExhausted(per_day)"
    assert row.ingredient_output is not None


def test_daily_quota_on_the_primary_alone_falls_back_and_the_run_completes():
    script = [
        per_day(), text(ingredient_json()),
        per_day(), text(price_json()),
        per_day(), text(planner().model_dump_json()),
    ]  # fmt: skip
    runner, store = make_runner(llm_factory=gemini_factory(script))
    run_id, row = run(runner)
    assert row.status == "completed" and row.failure_reason is None
    assert row.models_used == ["backup"] and row.fell_back is True
    steps = store.get_steps(run_id)
    assert all(s.model == "backup" and s.fell_back and s.requests == 2 for s in steps)
    assert row.requests == 6
    assert runner.events(run_id)[1].data["fell_back"] is True


def test_rate_limit_is_not_a_daily_quota_failure():
    runner, _ = make_runner(llm_factory=gemini_factory([per_minute()] * 3))
    _, row = run(runner)
    assert row.status == "failed" and row.failure_reason == "rate_limited"


def test_quota_exhausted_from_a_fake_client():
    runner, _ = make_runner(failing_script(1, QuotaExhausted("per_day")))
    assert run(runner)[1].failure_reason == "daily_quota"


def test_a_billing_stop_fails_the_run_with_the_daily_capacity_message():
    runner, _ = make_runner(failing_script(1, QuotaExhausted("billing")))
    row = run(runner)[1]
    assert row.failure_reason == "daily_quota" and "AI quota" in row.user_message
    assert "billing" not in row.user_message.lower()


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


@dataclass
class SlowFake(FakeLLMClient):
    clock: Clock = field(default_factory=Clock)
    seconds: float = 0.0

    def generate(self, *a, **kw):
        result = super().generate(*a, **kw)
        self.clock.t += self.seconds
        return result


def test_deadline_exceeded_fails_the_run_with_timeout_and_keeps_completed_steps():
    clock = Clock()

    def make(hooks):
        return SlowFake(
            happy_script(),
            on_request=hooks.on_request,
            before_request=hooks.before_request,
            clock=clock,
            seconds=200.0,
        )

    runner, store = make_runner(llm_factory=make, clock=clock)
    run_id, row = run(runner)
    assert row.status == "failed" and row.failure_reason == "timeout"
    assert step_names(store, run_id) == [
        ("ingredient", "completed")
    ]  # stopped before the next step
    assert row.ingredient_output is not None and "too long" in row.user_message
    assert row.latency_ms == 200_000


def test_deadline_is_configurable_and_recorded_with_the_run():
    clock = Clock()

    def make(hooks):
        return SlowFake(
            happy_script(),
            on_request=hooks.on_request,
            before_request=hooks.before_request,
            clock=clock,
            seconds=200.0,
        )

    runner, _ = make_runner(
        llm_factory=make, clock=clock, settings=RunnerSettings(deadline_seconds=500)
    )
    run_id, row = run(runner)
    assert row.status == "completed" and row.parameters["deadline_seconds"] == 500


class ExplodingSource:
    def search(self, term, store, limit, brand=None):
        raise RuntimeError("secret connection string leaked here")


def test_unexpected_errors_fail_the_run_as_internal_without_leaking_details():
    runner, store = make_runner(source=ExplodingSource())
    run_id, row = run(runner)
    assert row.status == "failed" and row.failure_reason == "internal"
    blob = json.dumps(
        [row.user_message, [s.error for s in store.get_steps(run_id)], row.model_dump(mode="json")]
    )
    assert "secret" not in blob and "RuntimeError" in store.get_steps(run_id)[1].error
    assert "Traceback" not in blob
    assert row.ingredient_output is not None


# ---- stale sweep and heartbeat ---------------------------------------------------------------


def test_stale_running_runs_are_failed_and_fresh_ones_left_alone():
    runner, store = make_runner()
    stale = store.create_run({}, {}, 9)
    fresh = store.create_run({}, {}, 9)
    queued = store.create_run({}, {}, 9)
    for rid in (stale, fresh):
        store.update_run(rid, status="running")
    store.runs[stale] = store.runs[stale].model_copy(
        update={"updated_at": datetime.now(UTC) - timedelta(minutes=6)}
    )
    store.runs[queued] = store.runs[queued].model_copy(
        update={"updated_at": datetime.now(UTC) - timedelta(minutes=60)}
    )
    assert runner.sweep_stale() == [stale]
    assert (
        store.get_run(stale).status == "failed" and store.get_run(stale).failure_reason == "stale"
    )
    assert store.get_run(stale).completed_at and store.get_run(stale).user_message
    assert store.get_run(fresh).status == "running" and store.get_run(queued).status == "queued"
    assert runner.sweep_stale() == []  # already failed: not swept twice
    assert runner.events(stale)[-1].type == "run_failed"


def test_each_step_refreshes_the_heartbeat():
    store = InMemoryRunStore()
    run_id = store.create_run({}, {}, 9)
    stamps = [store.get_run(run_id).updated_at]
    seq = store.start_step(run_id, "ingredient", "ingredient", 1, {})
    stamps.append(store.get_run(run_id).updated_at)
    store.finish_step(run_id, seq, status="completed")
    stamps.append(store.get_run(run_id).updated_at)
    assert stamps == sorted(stamps) and stamps[-1] > stamps[0]


# ---- events and replay -----------------------------------------------------------------------


def test_events_are_ordered_with_increasing_seq_and_short_summaries():
    runner, _ = make_runner()
    run_id, _ = run(runner)
    events = runner.events(run_id)
    assert [e.seq for e in events] == list(range(1, 8))
    assert [(e.type, e.name) for e in events] == [
        ("step_started", "ingredient"), ("step_completed", "ingredient"),
        ("step_started", "price"), ("step_completed", "price"),
        ("step_started", "planner"), ("step_completed", "planner"), ("run_completed", None),
    ]  # fmt: skip
    assert events[1].summary == "2 items from 1 dishes (0 cached)"
    assert events[1].data["model"] == "fake-model" and events[1].data["requests"] == 1
    assert events[-1].summary.startswith("No meaningful difference")


def test_replay_after_a_simulated_disconnect_returns_exactly_the_missing_events():
    runner, store = make_runner(script_fn=loopback_script)
    run_id, _ = run(runner)
    everything = runner.events(run_id)
    for seen in (0, 1, 4, 7, 11, 12):
        missed = runner.events(run_id, after_seq=seen)
        assert missed == everything[seen:], seen
        assert [e.seq for e in missed] == list(range(seen + 1, len(everything) + 1))
    assert runner.events(run_id, after_seq=len(everything)) == []
    streamed = list(follow_events(store, run_id, after_seq=5, sleep=lambda s: None))
    assert streamed == everything[5:]  # follow_events resumes the same way


def test_events_for_a_run_in_progress_are_a_stable_prefix_of_the_final_events():
    runner, store = make_runner(script_fn=loopback_script)
    run_id, _ = run(runner)
    full = events_from_state(store.get_run(run_id), store.get_steps(run_id))
    steps = store.get_steps(run_id)
    running = store.get_run(run_id).model_copy(update={"status": "running", "completed_at": None})
    for k in range(len(steps) + 1):
        partial = events_from_state(running, steps[:k])
        assert partial == full[: len(partial)]
    # a step that has started but not finished yields only its step_started
    unfinished = [*steps[:2], steps[2].model_copy(update={"status": "running"})]
    mid = events_from_state(running, unfinished)
    assert [e.type for e in mid[-1:]] == ["step_started"] and mid == full[: len(mid)]


def test_follow_events_waits_for_a_run_that_is_still_going():
    store = InMemoryRunStore()
    run_id = store.create_run({}, {}, 9)
    store.update_run(run_id, status="running")
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 1:  # the run makes progress while the client waits
            seq = store.start_step(run_id, "ingredient", "ingredient", 1, {})
            store.finish_step(run_id, seq, status="completed", summary="done")
            store.update_run(run_id, status="completed")

    got = list(follow_events(store, run_id, sleep=sleep))
    assert [e.type for e in got] == ["step_started", "step_completed", "run_completed"]
    assert len(sleeps) == 1
    assert list(follow_events(store, run_id, max_wait_seconds=0, sleep=sleep, after_seq=3)) == []


# ---- background execution ---------------------------------------------------------------------


def test_a_run_started_in_the_background_finishes_without_any_client():
    gate = threading.Event()

    class Gated(FakeLLMClient):
        def generate(self, *a, **kw):
            gate.wait(5)
            return super().generate(*a, **kw)

    runner, store = make_runner(
        llm_factory=lambda hooks: Gated(happy_script(), on_request=hooks.on_request)
    )
    run_id = runner.create_run(REQUEST, PlannerConfig(), AS_OF)
    thread = runner.submit(run_id)
    assert thread.is_alive() and store.get_run(run_id).status in ("queued", "running")
    gate.set()  # nobody is listening; the run carries on regardless
    thread.join(10)
    assert not thread.is_alive() and store.get_run(run_id).status == "completed"
    assert runner.events(run_id)[-1].type == "run_completed"


def test_start_creates_and_submits_in_one_call():
    runner, store = make_runner()
    run_id = runner.start(REQUEST, PlannerConfig(), AS_OF)
    for t in threading.enumerate():
        if t.name == f"run-{run_id}":
            t.join(10)
    assert store.get_run(run_id).status == "completed"


# ---- budget hook ------------------------------------------------------------------------------


def test_budget_hook_is_called_reserve_then_record_per_request_then_release():
    budget = Recorder()
    runner, _ = make_runner(budget=budget)
    run_id, _ = run(runner)
    assert budget.calls == [
        ("reserve", run_id, 9),
        ("record", run_id, "fake-model", 1),
        ("record", run_id, "fake-model", 1),
        ("record", run_id, "fake-model", 1),
        ("release", run_id),
    ]


def test_budget_is_released_even_when_the_run_fails():
    for script_fn, expected_records in (
        (failing_script(2, SchemaValidationFailed(["a", "b"], ["x"])), 2),
        (failing_script(1, QuotaExhausted("per_day")), 1),
    ):
        budget = Recorder()
        runner, _ = make_runner(script_fn, budget=budget)
        run_id, row = run(runner)
        assert row.status == "failed"
        assert [c[0] for c in budget.calls] == [
            "reserve",
            *["record"] * expected_records,
            "release",
        ]
    budget = Recorder()
    runner, _ = make_runner(source=ExplodingSource(), budget=budget)  # an unexpected error
    run(runner)
    assert budget.calls[0][0] == "reserve" and budget.calls[-1][0] == "release"


def test_budget_records_each_model_that_served_a_request():
    budget = Recorder()
    script = [
        per_day(), text(ingredient_json()),
        per_day(), text(price_json()),
        per_day(), text(planner().model_dump_json()),
    ]  # fmt: skip
    runner, _ = make_runner(llm_factory=gemini_factory(script), budget=budget)
    run(runner)
    models = [c[2] for c in budget.calls if c[0] == "record"]
    assert models == ["primary", "backup"] * 3


def test_noop_budget_guard_does_nothing():
    guard = NoopBudgetGuard()
    assert guard.reserve("r", 9) is None and guard.record("r", "m", 1) is None
    assert guard.release("r") is None


# ---- command line ----------------------------------------------------------------------------


def test_example_requests_are_valid():
    from pathlib import Path

    for name in ("request_1.json", "request_2.json"):
        request, config = load_request(Path(__file__).parents[1] / "examples" / name)
        assert request.meals
    assert load_request(Path(__file__).parents[1] / "examples" / "request_2.json")[
        1
    ].savings_threshold_dollars == D(10)


def test_cli_runs_a_request_file_and_prints_the_stored_run(tmp_path, capsys):
    path = tmp_path / "req.json"
    path.write_text(REQUEST.model_dump_json())
    runner, store = make_runner()
    code = main(["run", str(path), "--memory", "--as-of", "2026-10-14"], lambda args, as_of: runner)
    out = capsys.readouterr().out
    assert code == 0
    for heading in ("SHOPPING LIST", "PER-STORE COMPARISON", "RECOMMENDATION", "STEPS"):
        assert heading in out
    assert "bacon" in out and "Kingsway Streaky Bacon" in out and "fake-model" in out
    assert "No meaningful difference" in out
    (run_id,) = store.runs
    assert f"Run {run_id}" in out


def test_cli_exit_codes_and_stopped_output(tmp_path, capsys):
    path = tmp_path / "req.json"
    path.write_text(REQUEST.model_dump_json())
    runner, _ = make_runner(script_fn=stop_script)
    assert main(["run", str(path)], lambda a, d: runner) == 1
    out = capsys.readouterr().out
    assert "STOPPED:" in out and "durian" in out and "not_found" in out
    runner, _ = make_runner(failing_script(1, LLMUnavailable("down")))
    assert main(["run", str(path)], lambda a, d: runner) == 2
    assert "FAILED:" in capsys.readouterr().out


def test_render_works_from_a_stored_run_alone():
    runner, store = make_runner()
    run_id, row = run(runner)
    text_out = render(store.get_run(run_id), store.get_steps(run_id))
    assert text_out.startswith(f"Run {run_id}") and "requests: 3 of 9 reserved" in text_out
