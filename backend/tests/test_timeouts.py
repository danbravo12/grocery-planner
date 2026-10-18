"""Per-agent timeout for one model request: the numbers, the limits, and that each agent sets it."""

import pytest

from grocery_planner.llm import FakeLLMClient
from grocery_planner.llm.timeouts import (
    AGENT_TIMEOUT_MS,
    CAP_MS,
    FLOOR_MS,
    agent_timeout,
    current_timeout_ms,
    timeout_ms_for,
)
from tests.test_llm import GOOD, Out, run, text
from tests.test_orchestrator import happy_script, make_runner
from tests.test_orchestrator import run as run_plan


def test_the_chosen_values_follow_the_measured_p95_rule():
    # step 15b: ingredient 12 s, price 25 s; the planner's measured p95 (13.9 s) was inflated by
    # calls that hung until the old 20 s timeout and were retried, so it is set to 12 s by hand
    assert AGENT_TIMEOUT_MS == {"ingredient": 12_000, "price": 25_000, "planner": 12_000}
    assert all(FLOOR_MS <= v <= CAP_MS for v in AGENT_TIMEOUT_MS.values())
    assert (FLOOR_MS, CAP_MS) == (8_000, 25_000)


def test_unknown_agents_and_code_outside_any_agent_get_the_client_default():
    assert timeout_ms_for("somebody-else") == 20_000
    assert current_timeout_ms(20_000) == 20_000
    assert current_timeout_ms(7_000) == 7_000


def test_the_context_sets_and_restores_the_value_even_when_nested_or_failing():
    with agent_timeout("ingredient"):
        assert current_timeout_ms() == 12_000
        with agent_timeout("planner"):
            assert current_timeout_ms() == 12_000
        assert current_timeout_ms() == 12_000
    assert current_timeout_ms(1) == 1
    with pytest.raises(RuntimeError), agent_timeout("price"):
        raise RuntimeError("boom")
    assert current_timeout_ms(1) == 1


def test_environment_override_is_in_seconds_and_clamped_to_the_floor_and_cap(monkeypatch, caplog):
    monkeypatch.setenv("GEMINI_TIMEOUT_PLANNER_S", "18")
    assert timeout_ms_for("planner") == 18_000
    monkeypatch.setenv("GEMINI_TIMEOUT_PLANNER_S", "1")
    assert timeout_ms_for("planner") == FLOOR_MS
    monkeypatch.setenv("GEMINI_TIMEOUT_PLANNER_S", "300")
    assert timeout_ms_for("planner") == CAP_MS
    assert "clamped" in caplog.text
    monkeypatch.setenv("GEMINI_TIMEOUT_PLANNER_S", "soon")
    assert timeout_ms_for("planner") == 12_000  # not a number: the default stays
    monkeypatch.setenv("GEMINI_TIMEOUT_PLANNER_S", "")
    assert timeout_ms_for("planner") == 12_000
    assert timeout_ms_for("ingredient") == 12_000  # other agents are untouched


def test_the_gemini_client_sends_the_agent_timeout_and_keeps_20_s_elsewhere():
    h = run([text(GOOD)] * 4)
    h.client.generate("s", "u", Out)
    for agent, expected in (("ingredient", 12_000), ("price", 25_000), ("planner", 12_000)):
        with agent_timeout(agent):
            h.client.generate("s", "u", Out)
        assert h.sent[-1]["config"].http_options.timeout == expected
    assert h.sent[0]["config"].http_options.timeout == 20_000


def test_a_timeout_still_retries_once_and_then_falls_back_with_the_agent_value(monkeypatch):
    import httpx

    h = run([httpx.ReadTimeout("x"), httpx.ReadTimeout("x"), text(GOOD)], fallback_model="backup")
    with agent_timeout("ingredient"):
        r = h.client.generate("s", "u", Out)
    assert r.fell_back and len(h.sent) == 3  # same retry and fallback rules as before
    assert {c["config"].http_options.timeout for c in h.sent} == {12_000}


def test_each_agent_sets_its_own_timeout_during_a_real_run(monkeypatch):
    seen: list[int] = []
    real = FakeLLMClient.generate

    def spy(self, *args, **kw):
        seen.append(current_timeout_ms(-1))
        return real(self, *args, **kw)

    monkeypatch.setattr(FakeLLMClient, "generate", spy)
    monkeypatch.setattr(
        FakeLLMClient,
        "generate_with_tools",
        lambda self, *a, **k: (_ for _ in ()).throw(AssertionError("tool not expected")),
    )
    runner, _ = make_runner(happy_script)
    _, row = run_plan(runner)
    assert row.status == "completed"
    assert seen == [12_000, 25_000, 12_000]  # ingredient, price, planner
    assert current_timeout_ms(-1) == -1  # and nothing leaks out of the run
