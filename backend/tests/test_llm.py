import random

import httpx
import pytest
from google.genai import errors, types
from pydantic import BaseModel

from grocery_planner.llm import (
    FakeLLMClient,
    FakeToolCalls,
    GeminiClient,
    LLMError,
    LLMUnavailable,
    QuotaExhausted,
    SchemaValidationFailed,
    ToolLoopExceeded,
    ToolSpec,
    Usage,
)
from grocery_planner.llm.gemini import classify_429
from grocery_planner.llm.types import execute_tool


class Out(BaseModel):
    answer: str
    n: int


class AddIn(BaseModel):
    a: int
    b: int


def add(a: int, b: int) -> int:
    return a + b


def picky(a: int, b: int) -> int:
    raise ValueError("a must be positive")


ADD = ToolSpec("add", "Add two integers", AddIn, add)
GOOD = '{"answer": "ok", "n": 3}'
BAD = '{"answer": "ok"}'  # missing n


# ---- fake google-genai transport -----------------------------------------------------------


class FakeModels:
    def __init__(self, script):
        self.script, self.calls = list(script), []

    def generate_content(self, model, contents, config):
        self.calls.append({"model": model, "contents": list(contents), "config": config})
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


class FakeGenai:
    def __init__(self, script):
        self.models = FakeModels(script)


def text(body: str, inp=10, out=5, think=2):
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(content=types.Content(role="model", parts=[types.Part(text=body)]))
        ],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=inp, candidates_token_count=out, thoughts_token_count=think
        ),
    )


def tool_call(name: str, args: dict, inp=10, out=1, think=0):
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(
                    role="model",
                    parts=[types.Part(function_call=types.FunctionCall(name=name, args=args))],
                )
            )
        ],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=inp, candidates_token_count=out, thoughts_token_count=think
        ),
    )


def api_error(cls, code, status, quota_id=None, message="boom", retry_delay=None):
    details = []
    if quota_id:
        details.append({"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [{"quotaId": quota_id}]})  # fmt: skip
    if retry_delay:
        details.append(
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry_delay}
        )
    body = {"error": {"code": code, "status": status, "message": message, "details": details}}
    return cls(code, body)


def per_minute(retry_delay=None):
    return api_error(errors.ClientError, 429, "RESOURCE_EXHAUSTED",
                     "GenerateRequestsPerMinutePerProjectPerModel-FreeTier",
                     retry_delay=retry_delay)  # fmt: skip


def per_day():
    return api_error(errors.ClientError, 429, "RESOURCE_EXHAUSTED",
                     "GenerateRequestsPerDayPerProjectPerModel-FreeTier")  # fmt: skip


def unknown_429(message="Resource exhausted", retry_delay=None):
    return api_error(errors.ClientError, 429, "RESOURCE_EXHAUSTED", message=message,
                     retry_delay=retry_delay)  # fmt: skip


def unavailable():
    return api_error(errors.ServerError, 503, "UNAVAILABLE")


def deadline_exceeded():
    return api_error(errors.ServerError, 504, "DEADLINE_EXCEEDED")


class Harness:
    def __init__(self, script, **kw):
        self.genai = FakeGenai(script)
        self.sleeps: list[float] = []
        self.hook: list[Usage] = []
        kw.setdefault("model", "test-model")
        self.client = GeminiClient(
            genai_client=self.genai,
            sleep=self.sleeps.append,
            rng=random.Random(0),
            on_request=self.hook.append,
            **kw,
        )

    @property
    def sent(self):
        return self.genai.models.calls


def run(script, **kw):
    return Harness(script, **kw)


# ---- generate ------------------------------------------------------------------------------


def test_valid_first_try_and_request_config():
    h = run([text(GOOD)])
    r = h.client.generate("sys", "hello", Out)
    assert r.parsed == Out(answer="ok", n=3) and r.model == "test-model"
    assert (r.attempts, r.repair_used, r.tool_calls) == (1, False, [])
    assert r.usage == Usage(requests=1, input_tokens=10, output_tokens=5, thinking_tokens=2)
    cfg = h.sent[0]["config"]
    assert cfg.automatic_function_calling.disable is True
    assert cfg.response_mime_type == "application/json"
    assert cfg.response_json_schema == Out.model_json_schema()
    assert cfg.temperature == 0.1 and cfg.system_instruction == "sys"
    assert cfg.thinking_config.thinking_level == types.ThinkingLevel.MINIMAL
    assert cfg.http_options.timeout == 20_000 and cfg.tools is None


def test_thinking_level_is_configurable_and_can_be_left_default():
    h = run([text(GOOD)], thinking_level="low")
    h.client.generate("s", "u", Out)
    assert h.sent[0]["config"].thinking_config.thinking_level == types.ThinkingLevel.LOW
    h = run([text(GOOD)], thinking_level=None)
    h.client.generate("s", "u", Out)
    assert h.sent[0]["config"].thinking_config is None


def test_repair_succeeds_on_second_try():
    h = run([text(BAD), text(GOOD)])
    r = h.client.generate("s", "u", Out)
    assert r.parsed.n == 3 and r.repair_used is True
    assert r.usage.requests == r.attempts == 2
    repair_turn = h.sent[1]["contents"][-1].parts[0].text
    assert "n: Field required" in repair_turn
    assert h.sent[1]["contents"][-2].parts[0].text == BAD  # the bad reply is shown to the model


def test_repair_failure_raises_with_both_raw_outputs():
    h = run([text(BAD), text("not json")])
    with pytest.raises(SchemaValidationFailed) as e:
        h.client.generate("s", "u", Out)
    assert e.value.raw_outputs == [BAD, "not json"]
    assert len(h.sent) == 2 and len(h.hook) == 2  # only one repair attempt


def test_empty_reply_goes_through_repair():
    empty = types.GenerateContentResponse(candidates=[])
    h = run([empty, text(GOOD)])
    assert h.client.generate("s", "u", Out).repair_used is True


# ---- retries and 429s ----------------------------------------------------------------------


def test_default_model_and_env_override(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "")
    assert GeminiClient(genai_client=FakeGenai([])).model == "gemini-3.1-flash-lite"
    monkeypatch.setenv("GEMINI_MODEL", "some-other-model")
    assert GeminiClient(genai_client=FakeGenai([])).model == "some-other-model"


def test_5xx_retry_then_succeed_with_backoff():
    h = run([unavailable(), unavailable(), text(GOOD)])
    r = h.client.generate("s", "u", Out)
    assert r.parsed.n == 3 and r.attempts == 3
    assert len(h.sleeps) == 2
    assert 1.0 <= h.sleeps[0] <= 1.25 and 2.0 <= h.sleeps[1] <= 2.5


def test_5xx_gives_up_after_three_attempts():
    h = run([unavailable()] * 3)
    with pytest.raises(LLMUnavailable):
        h.client.generate("s", "u", Out)
    assert len(h.sent) == 3 and len(h.sleeps) == 2 and len(h.hook) == 3


@pytest.mark.parametrize("failure", [httpx.ReadTimeout("slow"), deadline_exceeded, TimeoutError])
def test_timeouts_and_504_get_one_retry_only(failure):
    make = failure if callable(failure) and not isinstance(failure, Exception) else lambda: failure
    h = run([make(), text(GOOD)])
    assert h.client.generate("s", "u", Out).attempts == 2  # a retry works
    h = run([make(), make(), text(GOOD)])
    with pytest.raises(LLMUnavailable):
        h.client.generate("s", "u", Out)
    assert len(h.sent) == 2 and len(h.sleeps) == 1 and len(h.hook) == 2  # third reply never sent


def test_per_day_429_is_not_retried():
    h = run([per_day(), text(GOOD)])
    with pytest.raises(QuotaExhausted) as e:
        h.client.generate("s", "u", Out)
    assert e.value.kind == "per_day"
    assert len(h.sent) == 1 and h.sleeps == [] and len(h.hook) == 1


def test_per_minute_429_is_retried():
    h = run([per_minute(), text(GOOD)])
    assert h.client.generate("s", "u", Out).attempts == 2
    assert len(h.sleeps) == 1


def test_per_minute_429_that_persists_raises_quota_exhausted():
    h = run([per_minute()] * 3)
    with pytest.raises(QuotaExhausted) as e:
        h.client.generate("s", "u", Out)
    assert e.value.kind == "per_minute" and len(h.sent) == 3


def test_retry_delay_hint_under_cap_is_used_instead_of_backoff():
    h = run([per_minute("7s"), text(GOOD)])
    assert h.client.generate("s", "u", Out).attempts == 2
    assert 7.0 <= h.sleeps[0] <= 7.5
    h = run([unknown_429(retry_delay="12.5s"), text(GOOD)])
    h.client.generate("s", "u", Out)
    assert 12.5 <= h.sleeps[0] <= 13.0


def test_retry_delay_hint_in_message_is_used():
    h = run([unknown_429("Quota hit. Please retry in 3.2s."), text(GOOD)])
    h.client.generate("s", "u", Out)
    assert 3.2 <= h.sleeps[0] <= 3.7


def test_retry_delay_at_the_cap_is_still_waited_out():
    h = run([per_minute("20s"), text(GOOD)])
    h.client.generate("s", "u", Out)
    assert 20.0 <= h.sleeps[0] <= 20.5


@pytest.mark.parametrize(
    "make", [lambda: per_minute("39s"), lambda: unknown_429(retry_delay="21s")]
)
def test_retry_delay_hint_over_cap_raises_at_once(make):
    h = run([make(), text(GOOD)])
    with pytest.raises(QuotaExhausted) as e:
        h.client.generate("s", "u", Out)
    assert e.value.kind == "per_minute"
    assert len(h.sent) == 1 and h.sleeps == []


def test_no_hint_falls_back_to_backoff():
    h = run([per_minute(), unknown_429(), text(GOOD)])
    h.client.generate("s", "u", Out)
    assert 1.0 <= h.sleeps[0] <= 1.25 and 2.0 <= h.sleeps[1] <= 2.5


def test_single_shot_sends_once_and_never_repairs():
    h = run([unavailable(), text(GOOD)], single_shot=True)
    with pytest.raises(LLMUnavailable):
        h.client.generate("s", "u", Out)
    assert len(h.sent) == 1
    h = run([text(BAD), text(GOOD)], single_shot=True)
    with pytest.raises(SchemaValidationFailed):
        h.client.generate("s", "u", Out)
    assert len(h.sent) == 1


def test_unknown_429_is_retried_then_raised():
    h = run([unknown_429()] * 3)
    with pytest.raises(QuotaExhausted) as e:
        h.client.generate("s", "u", Out)
    assert e.value.kind == "unknown" and len(h.sent) == 3


def test_other_client_errors_are_not_retried():
    h = run([api_error(errors.ClientError, 400, "INVALID_ARGUMENT"), text(GOOD)])
    with pytest.raises(LLMError):
        h.client.generate("s", "u", Out)
    assert len(h.sent) == 1


@pytest.mark.parametrize(
    "err, kind",
    [
        (per_day(), "per_day"),
        (per_minute(), "per_minute"),
        (unknown_429(), "unknown"),
        (unknown_429("requests per day"), "per_day"),
        (unknown_429("5 requests per minute"), "per_minute"),
    ],
)  # fmt: skip
def test_classify_429(err, kind):
    assert classify_429(err) == kind


# ---- tool loop -----------------------------------------------------------------------------


def with_tools(h, rounds=3, tools=(ADD,)):
    return h.client.generate_with_tools("s", "u", Out, list(tools), rounds)


def test_tool_loop_happy_path():
    h = run([tool_call("add", {"a": 2, "b": 3}), text(GOOD)])
    r = with_tools(h)
    assert r.parsed.n == 3 and r.attempts == r.usage.requests == 2
    (rec,) = r.tool_calls
    assert (rec.name, rec.args, rec.result_summary, rec.error) == (
        "add",
        {"a": 2, "b": 3},
        "5",
        None,
    )
    cfg = h.sent[0]["config"]
    decl = cfg.tools[0].function_declarations[0]
    assert decl.name == "add" and decl.parameters_json_schema == AddIn.model_json_schema()
    assert cfg.automatic_function_calling.disable is True
    second = h.sent[1]["contents"]
    assert second[1].role == "model" and second[1].parts[0].function_call.name == "add"
    assert second[2].parts[0].function_response.response == {"result": 5}


def test_unknown_tool_returns_error_to_model_and_counts_a_round():
    h = run([tool_call("nope", {}), tool_call("add", {"a": 1, "b": 1}), text(GOOD)])
    r = with_tools(h, rounds=2)
    assert r.attempts == 3
    sent_back = h.sent[1]["contents"][-1].parts[0].function_response.response
    assert "unknown tool 'nope'" in sent_back["error"] and "add" in sent_back["error"]
    assert r.tool_calls[0].error and r.tool_calls[1].error is None


def test_invalid_tool_args_return_error_to_model():
    h = run([tool_call("add", {"a": "x"}), text(GOOD)])
    r = with_tools(h)
    assert (
        "invalid arguments for 'add'"
        in h.sent[1]["contents"][-1].parts[0].function_response.response["error"]
    )
    assert r.tool_calls[0].error is not None and r.attempts == 2


def test_tool_loop_exceeded():
    h = run([tool_call("add", {"a": 1, "b": 2})] * 3 + [text(GOOD)])
    with pytest.raises(ToolLoopExceeded):
        with_tools(h, rounds=2)
    assert len(h.sent) == 3  # two rounds served, the third request was refused


def test_tool_value_error_goes_back_to_model_other_errors_propagate():
    tools = {"p": ToolSpec("p", "d", AddIn, picky)}
    response, record = execute_tool(tools, "p", {"a": 1, "b": 2}, lambda: 0.0)
    assert response == {"error": "a must be positive"} and record.error == "a must be positive"

    def boom(a, b):
        raise KeyError("bug")

    with pytest.raises(KeyError):
        execute_tool({"b": ToolSpec("b", "d", AddIn, boom)}, "b", {"a": 1, "b": 2}, lambda: 0.0)


# ---- request accounting --------------------------------------------------------------------


def test_requests_counted_across_retries_repair_and_tool_rounds():
    script = [
        unavailable(),  # 1: failed send
        tool_call("add", {"a": 1, "b": 2}, inp=10, out=1),  # 2: tool round
        per_minute(),  # 3: failed send
        tool_call("add", {"a": 3, "b": 4}, inp=20, out=1),  # 4: tool round
        text(BAD, inp=30, out=4, think=3),  # 5: invalid final answer
        text(GOOD, inp=40, out=5, think=2),  # 6: repaired
    ]
    h = run(script)
    r = with_tools(h, rounds=3)
    assert r.repair_used is True and len(r.tool_calls) == 2
    assert r.usage == Usage(requests=6, input_tokens=100, output_tokens=11, thinking_tokens=5)
    assert r.attempts == 6 and len(h.sent) == 6
    # the hook fired once per request, and its deltas add up to the result's usage
    assert len(h.hook) == 6 and all(d.requests == 1 for d in h.hook)
    assert sum(h.hook, Usage()) == r.usage
    assert [d.input_tokens for d in h.hook] == [0, 10, 0, 20, 30, 40]  # failed sends: no tokens


def test_hook_fires_for_failed_requests_too():
    h = run([per_day()])
    with pytest.raises(QuotaExhausted):
        h.client.generate("s", "u", Out)
    assert h.hook == [Usage(requests=1)]


# ---- fake client ---------------------------------------------------------------------------


def test_fake_client_returns_scripted_values_and_counts_requests():
    hook: list[Usage] = []
    fake = FakeLLMClient([Out(answer="a", n=1), QuotaExhausted("per_day")], on_request=hook.append)
    r = fake.generate("sys", "user", Out)
    assert r.parsed == Out(answer="a", n=1) and r.usage.requests == 1
    with pytest.raises(QuotaExhausted):
        fake.generate("sys", "user", Out)
    assert len(hook) == 1 and [c["user"] for c in fake.calls] == ["user", "user"]
    with pytest.raises(AssertionError):
        fake.generate("s", "u", Out)  # script exhausted


def test_fake_client_runs_tools_for_real():
    script = [FakeToolCalls([[("add", {"a": 2, "b": 2})], [("nope", {})]], Out(answer="x", n=4))]
    r = FakeLLMClient(script).generate_with_tools("s", "u", Out, [ADD], max_tool_rounds=3)
    assert r.parsed.n == 4 and r.attempts == 3
    assert [t.result_summary[:1] for t in r.tool_calls] == ["4", "u"] and r.tool_calls[1].error
    too_many = FakeLLMClient(
        [FakeToolCalls([[("add", {"a": 1, "b": 1})]] * 3, Out(answer="x", n=1))]
    )
    with pytest.raises(ToolLoopExceeded):
        too_many.generate_with_tools("s", "u", Out, [ADD], max_tool_rounds=2)
