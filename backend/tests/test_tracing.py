import json
import uuid
from decimal import Decimal as D

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from grocery_planner.llm import FakeLLMClient, GeminiClient, Usage
from grocery_planner.tracing import (
    NoopTracer,
    RecordingTracer,
    RunTraceInfo,
    current_app_env,
    would_cost,
)
from grocery_planner.tracing.base import SafeTracer
from tests.test_llm import FakeGenai, deadline_exceeded, per_day, text, unavailable
from tests.test_orchestrator import (
    SOURCE,
    SPLIT_REQUEST,
    SPLIT_SOURCE,
    happy_script,
    loopback_script,
    make_runner,
    planner,
    prices,
    run,
    split_script,
)

PRIMARY = "gemini-3.1-flash-lite"


def fake_factory(script_fn):
    def make(hooks):
        return FakeLLMClient(
            script_fn(), on_request=hooks.on_request, before_request=hooks.before_request,
            start_generation=hooks.start_generation, on_tool_call=hooks.on_tool_call,
        )  # fmt: skip

    return make


def gemini_factory(script, **kw):
    def make(hooks):
        return GeminiClient(
            genai_client=FakeGenai(list(script)), model=PRIMARY, fallback_model="backup-model",
            sleep=lambda s: None, on_request=hooks.on_request,
            before_request=hooks.before_request, fallback_allowed=hooks.fallback_allowed,
            on_quota_exhausted=hooks.on_quota_exhausted,
            start_generation=hooks.start_generation, on_tool_call=hooks.on_tool_call, **kw,
        )  # fmt: skip

    return make


def traced_runner(script_fn=happy_script, factory=None, source=SOURCE, **kw):
    tracer = RecordingTracer()
    runner, store = make_runner(
        llm_factory=factory or fake_factory(script_fn), source=source, tracer=tracer,
        primary_model="fake-model", app_env="dev", **kw,
    )  # fmt: skip
    return runner, store, tracer


def attrs(node):
    return node.attrs


# ---- the tree ---------------------------------------------------------------------------------


def test_default_tracer_is_a_noop_and_the_run_has_no_trace_id():
    runner, store = make_runner()
    assert isinstance(runner.tracer, SafeTracer) and isinstance(runner.tracer.inner, NoopTracer)
    _, row = run(runner)
    assert row.status == "completed" and row.langfuse_trace_id is None


def test_happy_path_tree_is_one_span_per_step_and_one_generation_per_request():
    runner, store, tracer = traced_runner()
    run_id, row = run(runner)
    root = tracer.root
    assert root.shape() == (
        "plan_run",
        [
            ("ingredient", [("llm_request", [])]),
            ("price", [("llm_request", [])]),
            ("planner", [("llm_request", [])]),
        ],
    )
    assert all(n.ended for n in root.walk())
    assert tracer.observations() == 7 <= 12  # trace + 3 steps + 3 generations
    assert row.langfuse_trace_id == f"recorded-{run_id}"
    # trace-level attributes
    assert root.attrs["app_env"] == "dev" and root.attrs["run_id"] == run_id
    assert root.attrs["tags"] == [
        "dev", "ingredient@3", "price@2", "planner@5", "model:fake-model",
    ]  # fmt: skip
    meta = root.attrs["metadata"]
    assert meta["as_of"] == "2026-10-14" and meta["meals"] == 1 and meta["dishes"] == 1
    assert meta["config"]["savings_threshold_dollars"] == "8"
    blob = json.dumps([root.attrs["tags"], meta, root.attrs["input"]]).lower()
    assert "visitor" not in blob and "ip_" not in blob and "cookie" not in blob
    # step spans carry the summary as output and a code-work summary in metadata
    ingredient, price, plan = root.children
    assert ingredient.attrs["output"] == "2 items from 1 dishes (0 cached)"
    assert ingredient.attrs["prompt_ref"] == "ingredient@3" and ingredient.attrs["attempt"] == 1
    assert (
        ingredient.attrs["metadata"]["cache_hits"] == 0
        and ingredient.attrs["metadata"]["items"] == 2
    )
    assert price.attrs["metadata"]["catalogue_searches"] == 4  # 2 items x 2 stores
    assert price.attrs["metadata"]["catalogue_candidates"] == 12
    assert price.attrs["prompt_ref"] == "price@2" and plan.attrs["prompt_ref"] == "planner@5"
    assert plan.attrs["output"].startswith("No meaningful difference")
    assert plan.attrs["metadata"]["tool_calls"] == 0 and plan.attrs["metadata"]["confidence"]
    assert (
        price.attrs["metadata"]["model"] == "fake-model"
        and price.attrs["metadata"]["requests"] == 1
    )
    # no span for the catalogue searches or the planner maths
    assert not [n for n in root.walk() if n.name in ("search", "packs_needed", "rule_verdict")]
    # the run's closing summary
    done = root.attrs["finish_metadata"]
    assert (
        root.attrs["status"] == "completed"
        and done["requests"] == 3
        and done["models"] == ["fake-model"]
    )
    assert done["loopback_used"] is False and "WOULD cost" in done["cost_note"]
    # a generation records its model, kind and attempt, and the prompt as given
    gen = ingredient.children[0]
    assert (gen.attrs["model"], gen.attrs["kind"], gen.attrs["attempt"]) == (
        "fake-model",
        "request",
        1,
    )
    assert gen.attrs["usage"].requests == 1 and gen.attrs["fell_back"] is False
    assert json.loads(gen.attrs["user"])["dishes"][0]["dish_name"] == "bacon and eggs"


def test_an_all_cached_ingredient_step_has_no_generation():
    runner, store, tracer = traced_runner()
    run(runner)  # fills the dish cache
    tracer.runs.clear()

    def script():  # the cache serves the ingredient step: only price and planner call the model
        return happy_script()[1:]

    runner.llm_factory = None
    runner._llm_factory = fake_factory(script)
    _, row = run(runner)
    ingredient = tracer.root.children[0]
    assert ingredient.name == "ingredient" and ingredient.children == []
    assert ingredient.attrs["metadata"]["cache_hits"] == 1
    assert ingredient.attrs["metadata"]["cache_misses"] == 0
    assert ingredient.attrs["output"] == "2 items from 1 dishes (1 cached)"
    assert tracer.observations() == 6 and row.requests == 2


def test_loopback_adds_reprice_and_planner_2_spans_with_their_own_generations():
    runner, store, tracer = traced_runner(loopback_script)
    run_id, row = run(runner)
    assert tracer.root.shape() == (
        "plan_run",
        [
            ("ingredient", [("llm_request", [])]),
            ("price", [("llm_request", [])]),
            ("planner", [("llm_request", [])]),
            ("reprice", [("llm_request", [])]),
            ("planner_2", [("llm_request", [])]),
        ],
    )
    reprice, planner_2 = tracer.root.children[3:]
    assert reprice.attrs["attempt"] == 2 and reprice.attrs["prompt_ref"] == "price@2"
    assert planner_2.attrs["prompt_ref"] == "planner@5"
    assert tracer.observations() == 11 <= 12
    assert tracer.root.attrs["finish_metadata"]["loopback_used"] is True


def test_what_if_split_calls_are_child_spans_of_the_planner_span():
    runner, store, tracer = traced_runner(split_script, source=SPLIT_SOURCE)
    run_id, row = run(runner, request=SPLIT_REQUEST)
    plan = tracer.root.children[2]
    assert plan.name == "planner"
    assert [(c.kind, c.name) for c in plan.children] == [
        ("generation", "llm_request"), ("tool", "what_if_split"), ("generation", "llm_request"),
    ]  # fmt: skip
    assert [c.attrs["kind"] for c in plan.children if c.kind == "generation"] == [
        "request", "tool_round",
    ]  # fmt: skip
    tool = plan.children[1]
    assert tool.attrs["args"] == {"assignments": [{"key": "meat", "store": "coles"}]}
    assert tool.attrs["error"] is None and "meets_threshold" in tool.attrs["summary"]
    assert tracer.observations() == 9 <= 12


def test_a_stopped_run_has_a_stop_span_and_no_planner():
    from tests.test_orchestrator import stop_script

    runner, store, tracer = traced_runner(stop_script)
    _, row = run(runner)
    assert row.status == "stopped"
    assert [c.name for c in tracer.root.children] == ["ingredient", "price", "stop"]
    stop = tracer.root.children[2]
    assert stop.children == [] and stop.attrs["output"] == "stopped: too_many_unmatched"
    assert stop.attrs["metadata"]["failed_items"] == 3
    assert tracer.root.attrs["status"] == "stopped"
    assert tracer.root.attrs["finish_metadata"]["stop_reason"] == "too_many_unmatched"


def test_a_failed_step_ends_its_span_with_the_error_class_only():
    from grocery_planner.llm import SchemaValidationFailed
    from tests.test_orchestrator import failing_script

    runner, _, tracer = traced_runner(failing_script(2, SchemaValidationFailed(["a", "b"], ["x"])))
    _, row = run(runner)
    plan = tracer.root.children[2]
    assert plan.attrs["error"] == "SchemaValidationFailed" and plan.ended
    assert tracer.root.attrs["status"] == "failed" and tracer.root.ended
    assert tracer.root.attrs["finish_metadata"]["failure_reason"] == "schema_validation"


# ---- retries, repairs and the fallback --------------------------------------------------------


def _ingredient_json(*ingredient_rows) -> str:
    def one(n, q, u, v):
        return {"name": n, "quantity_per_serving": q, "unit": u, "in_vocabulary": v}

    dish = {"dish_id": "d1", "ingredients": [one(*r) for r in ingredient_rows], "assumptions": []}
    return json.dumps({"dishes": [dish]})


GOOD_ING = _ingredient_json(("bacon", 100, "g", True), ("eggs", 2, "each", True))
BAD_ING = _ingredient_json(("bacon", 100, "g", True), ("eggs", 100, "g", True))  # wrong unit
PRICE_JSON = prices(
    ("i1", "col-eggs-01", "wol-eggs-01"), ("i2", "col-bacon-01", "wol-bacon-01")
).model_dump_json()
PLAN_JSON = planner().model_dump_json()


def test_a_retry_and_a_repair_are_separate_generations_under_one_span():
    script = [
        unavailable(),  # 1: the send fails
        text(BAD_ING, inp=1000, out=100),  # 2: the retry answers, but the unit is wrong
        text(GOOD_ING, inp=1200, out=110),  # 3: the repair
        text(PRICE_JSON, inp=2000, out=300),
        text(PLAN_JSON, inp=1500, out=100),
    ]
    runner, store, tracer = traced_runner(factory=gemini_factory(script))
    _, row = run(runner)
    assert row.status == "completed" and row.requests == 5
    ingredient = tracer.root.children[0]
    gens = ingredient.children
    assert [g.name for g in gens] == ["llm_request"] * 3
    assert [(g.attrs["kind"], g.attrs["attempt"]) for g in gens] == [
        ("request", 1), ("request", 2), ("repair", 1),
    ]  # fmt: skip
    assert gens[0].attrs["error"] == "ServerError(503)" and gens[0].attrs["usage"] is None
    assert gens[1].attrs["error"] is None and gens[1].attrs["usage"].input_tokens == 1000
    assert "failed validation" in gens[2].attrs["last_turn"]  # the repair prompt
    assert gens[2].attrs["usage"].output_tokens == 110
    assert tracer.observations() == 9 <= 12  # trace, 3 steps, 5 generations


def test_a_fallback_generation_shows_the_fallback_model_and_the_primary_failure():
    script = [
        per_day(), text(GOOD_ING),
        per_day(), text(PRICE_JSON),
        per_day(), text(PLAN_JSON),
    ]  # fmt: skip
    runner, store, tracer = traced_runner(factory=gemini_factory(script))
    _, row = run(runner)
    assert row.status == "completed" and row.fell_back is True
    for step in tracer.root.children:
        first, second = step.children
        assert first.attrs["model"] == PRIMARY and first.attrs["fell_back"] is False
        assert first.attrs["error"] == "ClientError(429)"
        assert second.attrs["model"] == "backup-model" and second.attrs["fell_back"] is True
        assert second.attrs["error"] is None
        assert step.attrs["metadata"]["fell_back"] is True
        assert step.attrs["metadata"]["model"] == "backup-model"
    assert tracer.root.attrs["finish_metadata"]["models"] == ["backup-model"]


def test_timeouts_are_recorded_as_error_generations_too():
    script = [
        deadline_exceeded(),
        deadline_exceeded(),
        text(GOOD_ING),
    ]  # primary dies, backup works
    runner, _, tracer = traced_runner(factory=gemini_factory(script))
    run(runner)
    gens = tracer.root.children[0].children
    assert [g.attrs["error"] for g in gens] == ["ServerError(504)", "ServerError(504)", None]
    assert [g.attrs["model"] for g in gens] == [PRIMARY, PRIMARY, "backup-model"]


# ---- tracing never breaks a run ---------------------------------------------------------------


class Boom:
    def __getattr__(self, name):
        def explode(*a, **k):
            raise RuntimeError(f"tracer exploded in {name}")

        return explode

    trace_id = "boom"


class ExplodingTracer:
    """start_run works but returns objects whose every method (and trace_id) raises."""

    enabled = True

    def start_run(self, info):
        return Boom()

    def trace_url(self, trace_id):
        raise RuntimeError("no url")


class ExplodingFromTheStart:
    enabled = True

    def start_run(self, info):
        raise RuntimeError("cannot even start")

    def trace_url(self, trace_id):
        raise RuntimeError("no url")


@pytest.mark.parametrize("tracer", [ExplodingTracer(), ExplodingFromTheStart()])
def test_a_tracer_that_raises_on_every_call_does_not_change_the_result(tracer):
    baseline_runner, baseline_store = make_runner()
    _, baseline = run(baseline_runner)
    runner, store = make_runner(tracer=tracer, llm_factory=fake_factory(happy_script))
    run_id, row = run(runner)
    assert row.status == "completed" and row.failure_reason is None
    assert row.requests == baseline.requests == 3
    assert row.ingredient_output == baseline.ingredient_output
    assert row.price_output == baseline.price_output
    assert row.planner_output == baseline.planner_output
    assert [s.name for s in store.get_steps(run_id)] == ["ingredient", "price", "planner"]
    assert runner.tracer.trace_url("x") is None


def test_a_tracer_that_raises_does_not_hide_a_real_failure_either():
    from grocery_planner.llm import LLMUnavailable
    from tests.test_orchestrator import failing_script

    runner, _ = make_runner(
        tracer=ExplodingTracer(), llm_factory=fake_factory(failing_script(1, LLMUnavailable("x")))
    )
    _, row = run(runner)
    assert row.status == "failed" and row.failure_reason == "model_unavailable"


def test_a_failing_start_generation_does_not_stop_a_request():
    def bad_start(info):
        raise RuntimeError("boom")

    client = GeminiClient(
        genai_client=FakeGenai([text('{"answer": "ok", "n": 3}')]),
        model=PRIMARY, fallback_model=None, start_generation=bad_start,
    )  # fmt: skip
    from tests.test_llm import Out

    assert client.generate("s", "u", Out).parsed.n == 3


# ---- would-be cost ------------------------------------------------------------------------------


def test_paid_tier_cost_uses_the_price_of_the_model_that_served_the_request():
    u = Usage(requests=1, input_tokens=1_000_000, output_tokens=500_000, thinking_tokens=500_000)
    cheap, dear = would_cost("gemini-3.1-flash-lite", u), would_cost("gemini-3.5-flash-lite", u)
    assert (cheap.input, cheap.output, cheap.total) == (D("0.25"), D("1.50"), D("1.75"))
    assert (dear.input, dear.output, dear.total) == (D("0.30"), D("2.50"), D("2.80"))
    assert would_cost("some-other-model", u) is None and would_cost(None, u) is None
    assert would_cost(PRIMARY, Usage(requests=1)).total == 0  # a failed request costs nothing


def test_the_run_stores_its_would_be_cost_and_the_free_tier_charges_nothing():
    script = [
        text(GOOD_ING, inp=1000, out=100, think=0),
        text(PRICE_JSON, inp=2000, out=300, think=0),
        text(PLAN_JSON, inp=1500, out=100, think=0),
    ]
    runner, store, tracer = traced_runner(factory=gemini_factory(script))
    _, row = run(runner)
    # 4500 input tokens at $0.25/M plus 500 output tokens at $1.50/M
    assert row.would_cost_usd == D("0.001125") + D("0.00075") and row.cost_usd == 0
    done = tracer.root.attrs["finish_metadata"]
    assert done["would_cost_usd_paid_tier"] == pytest.approx(0.001875)
    assert tracer.root.children[1].attrs["metadata"]["would_cost_usd_paid_tier"] == pytest.approx(
        0.00095
    )


def test_a_fallback_request_is_priced_at_the_fallback_models_rate():
    script = [per_day(), text(GOOD_ING, inp=1_000_000, out=0)]  # served by backup-model: unpriced
    runner, _, _ = traced_runner(factory=gemini_factory(script))
    _, row = run(runner)
    assert row.would_cost_usd == 0  # no price known for "backup-model", so nothing is counted


# ---- the real Langfuse tracer, with the network replaced by an in-memory exporter ----------


def langfuse_runner(script_fn=happy_script, factory=None):
    from langfuse import Langfuse

    from grocery_planner.tracing.langfuse_tracer import LangfuseTracer

    exporter = InMemorySpanExporter()
    client = Langfuse(
        public_key=f"pk-lf-{uuid.uuid4().hex}", secret_key="sk-lf-test", base_url="http://127.0.0.1:9",
        span_exporter=exporter, flush_at=1,
    )  # fmt: skip
    runner, store = make_runner(
        llm_factory=factory or fake_factory(script_fn), tracer=LangfuseTracer(client),
        primary_model="fake-model", app_env="eval",
    )  # fmt: skip
    return runner, store, exporter, client


def spans_by_name(exporter):
    return {s.name: s for s in exporter.get_finished_spans()}


def test_langfuse_tracer_emits_the_expected_spans_and_attributes():
    runner, store, exporter, client = langfuse_runner()
    run_id, row = run(runner)
    spans = exporter.get_finished_spans()
    by_name = {}
    for s in spans:
        by_name.setdefault(s.name, []).append(s)
    assert sorted((n, len(v)) for n, v in by_name.items()) == [
        ("ingredient", 1), ("llm_request", 3), ("plan_run", 1), ("planner", 1), ("price", 1),
    ]  # fmt: skip
    root = by_name["plan_run"][0]
    trace_id = client.create_trace_id(seed=run_id)
    assert row.langfuse_trace_id == trace_id == f"{root.context.trace_id:032x}"
    assert all(s.context.trace_id == root.context.trace_id for s in spans)
    a = dict(root.attributes)
    assert a["langfuse.trace.name"] == "plan_run" and a["langfuse.environment"] == "eval"
    assert set(a["langfuse.trace.tags"]) == {
        "eval", "ingredient@3", "price@2", "planner@5", "model:fake-model",
    }  # fmt: skip
    assert json.loads(a["langfuse.observation.metadata.config"])["savings_threshold_dollars"] == "8"
    # parenting: generations sit under their step spans, steps under the root
    ids = {s.name: s.context.span_id for s in spans if s.name != "llm_request"}
    parents = sorted(s.parent.span_id for s in by_name["llm_request"])
    assert parents == sorted([ids["ingredient"], ids["price"], ids["planner"]])
    for name in ("ingredient", "price", "planner"):
        assert by_name[name][0].parent.span_id == root.context.span_id
    # a generation: type, model, usage, the prompt by name, and no visitor or IP anywhere
    gen = next(s for s in by_name["llm_request"] if "ingredient@3" in str(dict(s.attributes)))
    g = dict(gen.attributes)
    assert g["langfuse.observation.type"] == "generation"
    assert g["langfuse.observation.model.name"] == "fake-model"
    assert json.loads(g["langfuse.observation.usage_details"]) == {
        "input": 0,
        "output": 0,
        "thinking": 0,
    }
    inp = json.loads(g["langfuse.observation.input"])
    assert inp["system_prompt"] == "ingredient@3" and "dishes" in json.loads(inp["user"])
    assert "You are the ingredient step" not in g["langfuse.observation.input"]  # not the full text
    everything = json.dumps([dict(s.attributes) for s in spans], default=str).lower()
    assert "visitor" not in everything and '"ip"' not in everything
    client.shutdown()


def test_langfuse_generation_records_cost_tokens_and_errors():
    script = [
        unavailable(), text(GOOD_ING, inp=1000, out=100, think=0),
        text(PRICE_JSON, inp=2000, out=300, think=0), text(PLAN_JSON, inp=1500, out=100, think=0),
    ]  # fmt: skip
    runner, _, exporter, client = langfuse_runner(factory=gemini_factory(script))
    run(runner)
    gens = [s for s in exporter.get_finished_spans() if s.name == "llm_request"]
    failed = next(
        s for s in gens if dict(s.attributes).get("langfuse.observation.level") == "ERROR"
    )
    assert dict(failed.attributes)["langfuse.observation.status_message"] == "ServerError(503)"
    ok = next(s for s in gens if s is not failed and "1000" in str(dict(s.attributes)))
    a = dict(ok.attributes)
    assert json.loads(a["langfuse.observation.usage_details"])["input"] == 1000
    cost = json.loads(a["langfuse.observation.cost_details"])
    assert cost["input"] == pytest.approx(0.00025) and cost["output"] == pytest.approx(0.00015)
    assert cost["total"] == pytest.approx(0.0004)
    assert "WOULD cost" in a["langfuse.observation.metadata.cost_note"]
    assert dict(ok.attributes)["langfuse.observation.metadata.attempt"] == 2
    client.shutdown()


def test_langfuse_tool_spans_hang_off_the_planner_span():
    from langfuse import Langfuse

    from grocery_planner.tracing.langfuse_tracer import LangfuseTracer

    exporter = InMemorySpanExporter()
    client = Langfuse(
        public_key=f"pk-lf-{uuid.uuid4().hex}", secret_key="sk-lf-test", base_url="http://127.0.0.1:9",
        span_exporter=exporter, flush_at=1,
    )  # fmt: skip
    runner, _ = make_runner(
        llm_factory=fake_factory(split_script), source=SPLIT_SOURCE, tracer=LangfuseTracer(client)
    )
    run(runner, request=SPLIT_REQUEST)
    spans = exporter.get_finished_spans()
    tool = next(s for s in spans if s.name == "what_if_split")
    planner_span = next(s for s in spans if s.name == "planner")
    assert tool.parent.span_id == planner_span.context.span_id
    assert dict(tool.attributes)["langfuse.observation.type"] == "tool"
    assert len(spans) == 9 <= 12
    client.shutdown()


def test_tracer_from_env_is_off_without_keys_or_when_disabled(monkeypatch):
    from grocery_planner.tracing import tracer_from_env

    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "")
    assert isinstance(tracer_from_env(), NoopTracer)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-x")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-x")
    monkeypatch.setenv("LANGFUSE_ENABLED", "false")
    assert isinstance(tracer_from_env(), NoopTracer)


@pytest.mark.parametrize(
    "value, expected",
    [("DEV", "dev"), ("Eval", "eval"), (" prod ", "prod"), ("staging", "dev"), ("", "dev")],
)
def test_app_env_is_dev_eval_or_prod(monkeypatch, value, expected):
    monkeypatch.setenv("APP_ENV", value)
    assert current_app_env() == expected


def test_run_trace_info_has_no_visitor_field():
    assert not {"visitor", "visitor_id", "ip"} & set(RunTraceInfo.__dataclass_fields__)
