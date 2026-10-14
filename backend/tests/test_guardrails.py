import threading
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import psycopg
import pytest

from grocery_planner.config import GUARDRAILS, GuardrailConfig
from grocery_planner.db import CONNECT_KWARGS, db_url
from grocery_planner.guardrails import (
    RESERVE_SQL,
    InMemoryDayBudget,
    PostgresDayBudget,
    next_reset,
    pacific_day,
)
from grocery_planner.guardrails.budget import IpHasher
from grocery_planner.guardrails.clock import PACIFIC, SYDNEY
from grocery_planner.llm import (
    BudgetExhausted,
    GeminiClient,
    LLMUnavailable,
)
from grocery_planner.llm.ratelimit import RateLimiter
from grocery_planner.orchestrator import BudgetUnavailable, InMemoryRunStore, PostgresRunStore
from tests.test_llm import GOOD, FakeGenai, Out, per_day, text, unavailable

PRIMARY, BACKUP = "itest-primary", "itest-fallback"
SMALL = GuardrailConfig(daily_limit=30, admission_limit=20)
START = datetime(2099, 3, 10, 18, 0, tzinfo=UTC)  # 11:00 Pacific, far from any real counter


class Clock:
    def __init__(self, now=START):
        self.now = now

    def __call__(self):
        return self.now


@dataclass
class Env:
    kind: str
    clock: Clock
    cfg: GuardrailConfig
    store: object
    budget: object
    run_ids: list

    def new_run(self) -> str:
        run_id = self.store.create_run({}, {}, 9)
        self.run_ids.append(run_id)
        return run_id

    def usage(self, model=PRIMARY):
        return {u.model: u for u in self.budget.snapshot()}[model]


def make_env(kind, cfg=SMALL) -> Env:
    clock = Clock()
    if kind == "memory":
        store, budget = InMemoryRunStore(), InMemoryDayBudget(PRIMARY, BACKUP, cfg, clock, "s3cret")
    else:
        store = PostgresRunStore(db_url())
        budget = PostgresDayBudget(db_url(), PRIMARY, BACKUP, cfg, clock, "s3cret")
    return Env(kind, clock, cfg, store, budget, [])


def cleanup(env: Env) -> None:
    if env.kind == "memory":
        return
    with psycopg.connect(db_url(), **CONNECT_KWARGS) as conn:
        conn.execute("DELETE FROM usage_counters WHERE day >= '2099-01-01'")
        if env.run_ids:
            conn.execute("DELETE FROM runs WHERE id = ANY(%s::uuid[])", (env.run_ids,))
    env.budget.close()
    env.store.close()


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.integration)])
def env(request):
    if request.param == "postgres":
        from grocery_planner.db.migrate import migrate

        migrate(db_url())
    e = make_env(request.param)
    yield e
    cleanup(e)


# ---- configuration and the clock -----------------------------------------------------------


def test_default_limits_are_70_percent_of_500_with_a_15_percent_buffer():
    assert (GUARDRAILS.daily_limit, GUARDRAILS.admission_limit) == (350, 298)
    assert GuardrailConfig.from_free_tier() == GUARDRAILS
    assert (GUARDRAILS.requests_per_minute, GUARDRAILS.visitor_runs_per_day) == (12, 5)
    assert (GUARDRAILS.ip_runs_per_day, GUARDRAILS.max_dishes) == (20, 20)


def test_the_pacific_day_changes_at_midnight_pacific_not_utc():
    before = datetime(2026, 10, 14, 6, 59, 59, tzinfo=UTC)  # 23:59:59 PDT on 13 Oct
    after = datetime(2026, 10, 14, 7, 0, 0, tzinfo=UTC)  # 00:00:00 PDT on 14 Oct
    assert pacific_day(before) == date(2026, 10, 13) and pacific_day(after) == date(2026, 10, 14)
    assert before.astimezone(PACIFIC).strftime("%H:%M:%S") == "23:59:59"
    assert after.astimezone(PACIFIC).strftime("%H:%M:%S") == "00:00:00"
    # the same instants given in other timezones land on the same Pacific days
    assert pacific_day(before.astimezone(SYDNEY)) == date(2026, 10, 13)


def test_reset_time_is_the_next_midnight_pacific_in_utc_and_sydney():
    now = datetime(2026, 10, 14, 6, 59, 59, tzinfo=UTC)
    reset = next_reset(now)
    assert reset == datetime(2026, 10, 14, 7, 0, tzinfo=UTC)  # PDT is UTC-7
    assert reset.astimezone(SYDNEY).strftime("%Y-%m-%d %H:%M %Z") == "2026-10-14 18:00 AEDT"
    assert next_reset(datetime(2026, 10, 14, 7, 0, tzinfo=UTC)) == datetime(
        2026, 10, 15, 7, 0, tzinfo=UTC
    )
    winter = next_reset(datetime(2026, 12, 1, 12, 0, tzinfo=UTC))  # PST is UTC-8
    assert winter == datetime(2026, 12, 2, 8, 0, tzinfo=UTC)
    assert winter.astimezone(SYDNEY).hour == 19 and winter.astimezone(PACIFIC).hour == 0


def test_availability_reports_reset_in_both_timezones(env):
    a = env.budget.availability()
    assert a.available and a.reason is None and a.day == date(2099, 3, 10)
    env.budget.quota_exhausted(PRIMARY, "per_day")
    a = env.budget.availability()
    assert not a.available and a.reason == "google_quota"
    assert a.reset_at_utc == datetime(2099, 3, 11, 7, 0, tzinfo=UTC)  # PDT in March 2099
    assert a.reset_at_sydney == a.reset_at_utc.astimezone(SYDNEY)
    assert "2099-03-11 07:00 UTC" in a.message and "Sydney time" in a.message
    env.clock.now += timedelta(days=1)  # the next Pacific day starts clean
    assert env.budget.availability().available


# ---- reserve, record, release ----------------------------------------------------------------


def test_reserve_adds_to_reserved_and_record_moves_it_to_used(env):
    run = env.new_run()
    env.budget.reserve(run, 9)
    assert (env.usage().used, env.usage().reserved) == (0, 9)
    env.budget.record(run, PRIMARY, 4)
    assert (env.usage().used, env.usage().reserved) == (4, 5)
    env.budget.record(run, PRIMARY, 5)
    assert (env.usage().used, env.usage().reserved) == (9, 0)
    env.budget.record(run, PRIMARY, 2)  # past the estimate: used grows, reserved stays at 0
    assert (env.usage().used, env.usage().reserved) == (11, 0)


def test_release_returns_the_unused_reservation_and_is_safe_to_repeat(env):
    run = env.new_run()
    env.budget.reserve(run, 9)
    env.budget.record(run, PRIMARY, 4)
    env.budget.release(run)
    assert (env.usage().used, env.usage().reserved) == (4, 0)
    env.budget.release(run)
    env.budget.release(env.new_run())  # a run that never reserved
    assert (env.usage().used, env.usage().reserved) == (4, 0)


def test_requests_served_by_the_fallback_count_on_its_own_counter(env):
    run = env.new_run()
    env.budget.reserve(run, 9)
    env.budget.record(run, BACKUP, 3)
    assert (env.usage(BACKUP).used, env.usage(PRIMARY).used) == (3, 0)
    assert env.usage(PRIMARY).reserved == 6  # the primary reservation is not needed for them
    env.budget.release(run)
    assert env.usage(PRIMARY).reserved == 0


def test_a_run_that_spans_midnight_pacific_returns_its_reservation_to_the_day_it_took(env):
    run = env.new_run()
    env.budget.reserve(run, 9)
    env.clock.now += timedelta(days=1)
    env.budget.record(run, PRIMARY, 2)  # counted on the new day
    assert env.usage().used == 2
    env.budget.release(run)  # the old day's reserved goes back to zero
    env.clock.now -= timedelta(days=1)
    assert (env.usage().used, env.usage().reserved) == (0, 0)


def test_twenty_threads_reserving_at_once_never_overshoot_admission(env):
    runs = [env.new_run() for _ in range(20)]
    results = []
    start = threading.Barrier(20)

    def worker(run):
        start.wait()
        try:
            env.budget.reserve(run, 9)
            results.append("ok")
        except BudgetUnavailable as e:
            results.append(e.reason)

    threads = [threading.Thread(target=worker, args=(r,)) for r in runs]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert len(results) == 20
    assert results.count("ok") == 2  # 2 x 9 = 18 fits in 20; a third (27) cannot
    assert set(results) == {"ok", "busy"}  # refusals only because of other runs' reservations
    assert env.usage().reserved == 18 <= env.cfg.admission_limit
    assert not env.usage().day_stopped  # nothing is used yet: it is "busy", not a stopped day


def test_new_runs_are_refused_at_the_admission_limit_but_in_flight_runs_can_finish(env):
    a, b, c = env.new_run(), env.new_run(), env.new_run()
    env.budget.reserve(a, 9)
    env.budget.reserve(b, 9)
    env.budget.record(a, PRIMARY, 9)
    env.budget.record(b, PRIMARY, 9)  # used 18, nothing reserved
    with pytest.raises(BudgetUnavailable) as e:
        env.budget.reserve(c, 9)  # 18 + 9 > 20
    assert e.value.reason == "day_budget" and env.usage().day_stopped
    assert not env.budget.availability().available
    assert env.usage().reserved == 0  # the refusal reserved nothing
    # run a is still going: it may use the buffer up to the daily limit
    env.budget.record(a, PRIMARY, 11)
    assert env.usage().used == 29
    env.budget.before_request(PRIMARY)  # 29 < 30: one more request is fine
    env.budget.record(a, PRIMARY, 1)
    with pytest.raises(BudgetExhausted) as e2:
        env.budget.before_request(PRIMARY)  # used == daily limit
    assert e2.value.model == PRIMARY and e2.value.kind == "per_day"


def test_busy_is_not_a_day_stop_and_clears_when_a_run_releases(env):
    a, b, c = env.new_run(), env.new_run(), env.new_run()
    env.budget.reserve(a, 9)
    env.budget.reserve(b, 9)
    with pytest.raises(BudgetUnavailable) as e:
        env.budget.reserve(c, 9)
    assert e.value.reason == "busy" and env.budget.availability().available
    env.budget.release(a)
    env.budget.reserve(c, 9)  # room again


def test_the_day_stops_when_the_primary_cannot_admit_even_a_small_run(env):
    run = env.new_run()
    env.budget.reserve(run, 9)
    env.budget.record(run, PRIMARY, 18)  # 18 + 3 (the smallest run) > 20
    assert env.usage().day_stopped and env.usage().stop_reason == "day_budget"
    assert env.budget.availability().reason == "day_budget"


def test_google_per_day_quota_on_the_primary_stops_the_day_whatever_our_counter_says(env):
    assert env.budget.availability().available and env.usage().used == 0
    client = GeminiClient(
        genai_client=FakeGenai([per_day(), text(GOOD)]),
        model=PRIMARY, fallback_model=BACKUP, sleep=lambda s: None,
        before_request=env.budget.before_request, fallback_allowed=env.budget.fallback_allowed,
        on_quota_exhausted=env.budget.quota_exhausted,
        on_request=lambda u: env.budget.record(env.new_run(), u.model, u.requests),
    )  # fmt: skip
    result = client.generate("s", "u", Out)
    assert result.fell_back and result.model == BACKUP
    a = env.budget.availability()
    assert not a.available and a.reason == "google_quota" and env.usage().used == 1
    # later calls do not even try the primary: they go straight to the fallback
    fake = FakeGenai([text(GOOD)])
    client2 = GeminiClient(
        genai_client=fake, model=PRIMARY, fallback_model=BACKUP, sleep=lambda s: None,
        before_request=env.budget.before_request, fallback_allowed=env.budget.fallback_allowed,
        on_quota_exhausted=env.budget.quota_exhausted,
    )  # fmt: skip
    assert client2.generate("s", "u", Out).model == BACKUP
    assert [c["model"] for c in fake.models.calls] == [BACKUP]


def test_an_exhausted_fallback_is_disabled_for_the_rest_of_the_day(env):
    assert env.budget.fallback_allowed()
    run = env.new_run()
    env.budget.record(run, BACKUP, 30)  # its own daily limit
    assert not env.budget.fallback_allowed() and env.usage(BACKUP).day_stopped
    fake = FakeGenai([unavailable()] * 3 + [text(GOOD)])
    client = GeminiClient(
        genai_client=fake, model=PRIMARY, fallback_model=BACKUP, sleep=lambda s: None,
        fallback_allowed=env.budget.fallback_allowed,
    )  # fmt: skip
    with pytest.raises(LLMUnavailable):  # behaves as if no fallback were configured
        client.generate("s", "u", Out)
    assert [c["model"] for c in fake.models.calls] == [PRIMARY] * 3
    env.clock.now += timedelta(days=1)
    assert env.budget.fallback_allowed()  # a new Pacific day


def test_google_per_day_quota_on_the_fallback_disables_it(env):
    client = GeminiClient(
        genai_client=FakeGenai([per_day(), per_day()]), model=PRIMARY, fallback_model=BACKUP,
        sleep=lambda s: None, before_request=env.budget.before_request,
        fallback_allowed=env.budget.fallback_allowed, on_quota_exhausted=env.budget.quota_exhausted,
    )  # fmt: skip
    from grocery_planner.llm import QuotaExhausted

    with pytest.raises(QuotaExhausted):
        client.generate("s", "u", Out)
    assert env.usage(PRIMARY).day_stopped and env.usage(BACKUP).day_stopped
    assert not env.budget.fallback_allowed()


def test_the_primary_budget_running_out_falls_back_to_the_other_model(env):
    run = env.new_run()
    env.budget.record(run, PRIMARY, 30)
    fake = FakeGenai([text(GOOD)])
    client = GeminiClient(
        genai_client=fake, model=PRIMARY, fallback_model=BACKUP, sleep=lambda s: None,
        before_request=env.budget.before_request, fallback_allowed=env.budget.fallback_allowed,
    )  # fmt: skip
    result = client.generate("s", "u", Out)
    assert result.fell_back and result.fallback_reason == "per_day quota"
    assert [c["model"] for c in fake.models.calls] == [BACKUP]  # nothing was sent to the primary


# ---- per-visitor and per-IP limits ------------------------------------------------------------


def test_five_runs_per_visitor_per_day(env):
    for _ in range(5):
        assert env.budget.admit("visitor-1", "10.0.0.1").ok
    refused = env.budget.admit("visitor-1", "10.0.0.1")
    assert not refused.ok and refused.reason == "visitor_limit"
    assert env.budget.admit("visitor-2", "10.0.0.1").ok  # another visitor, same IP
    env.clock.now += timedelta(days=1)
    assert env.budget.admit("visitor-1", "10.0.0.1").ok  # a new Pacific day


def test_twenty_runs_per_ip_per_day_and_a_refusal_counts_nothing(env):
    for n in range(20):
        assert env.budget.admit(f"v{n}", "203.0.113.9").ok
    refused = env.budget.admit("v-new", "203.0.113.9")
    assert not refused.ok and refused.reason == "ip_limit"
    # the refused visitor was not charged for the attempt: they can still use another IP 5 times
    for _ in range(5):
        assert env.budget.admit("v-new", "198.51.100.7").ok
    assert env.budget.admit("v-new", "198.51.100.7").reason == "visitor_limit"


def test_concurrent_visitors_never_exceed_the_limit(env):
    start = threading.Barrier(12)
    results = []

    def worker():
        start.wait()
        results.append(env.budget.admit("same-visitor", "192.0.2.1").ok)

    threads = [threading.Thread(target=worker) for _ in range(12)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert results.count(True) == 5 and results.count(False) == 7


def test_the_ip_is_stored_only_as_a_salted_hash(env):
    ip = "198.51.100.77"
    env.budget.admit("visitor-x", ip)
    if env.kind == "memory":
        stored = [str(k) for k in env.budget.counters]
    else:
        with psycopg.connect(db_url(), **CONNECT_KWARGS) as conn:
            stored = [
                f"{r['scope']}|{r['scope_key']}|{r['model']}"
                for r in conn.execute("SELECT scope, scope_key, model FROM usage_counters")
            ]
    assert stored and not any(ip in row for row in stored)
    hasher = IpHasher("s3cret")
    assert any(hasher.hash(ip, pacific_day(START)) in row for row in stored)


def test_ip_hash_depends_on_the_day_and_the_secret():
    h = IpHasher("secret-a")
    d1, d2 = date(2026, 10, 13), date(2026, 10, 14)
    assert h.hash("1.2.3.4", d1) == h.hash("1.2.3.4", d1)
    assert h.hash("1.2.3.4", d1) != h.hash("1.2.3.4", d2)  # the salt rotates daily
    assert h.hash("1.2.3.4", d1) != IpHasher("secret-b").hash("1.2.3.4", d1)
    assert h.hash("1.2.3.4", d1) != h.hash("1.2.3.5", d1)
    assert "1.2.3.4" not in h.hash("1.2.3.4", d1) and len(h.hash("1.2.3.4", d1)) == 64


# ---- the reserve statement ---------------------------------------------------------------------


def test_reserve_is_a_single_conditional_update():
    sql = " ".join(RESERVE_SQL.split())
    assert sql.startswith("UPDATE usage_counters SET reserved = reserved + %(est)s")
    assert "used + reserved + %(est)s <= %(admission)s" in sql and "RETURNING" in sql
    assert "NOT day_stopped" in sql and "SELECT" not in sql  # no read-then-write


# ---- per-minute limiter -----------------------------------------------------------------------


class FakeTime:
    def __init__(self):
        self.t = 1000.0
        self.slept = []

    def clock(self):
        return self.t

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += seconds


def test_the_limiter_waits_rather_than_exceed_the_per_minute_limit():
    ft = FakeTime()
    limiter = RateLimiter(12, 30.0, ft.clock, ft.sleep)
    start = ft.t
    stamps = []
    for _ in range(12):
        limiter.acquire("m")
        stamps.append(ft.t)
        ft.t += 1  # one request a second
    assert ft.slept == []  # twelve fit in the first minute
    ft.t = start + 40  # the 13th arrives 40 s in: it must wait for the first to leave the window
    limiter.acquire("m")
    stamps.append(ft.t)
    assert ft.slept == [pytest.approx(20)] and stamps[-1] == pytest.approx(start + 60)
    for _ in range(5):  # keep going: never more than 12 in any 60 second span
        limiter.acquire("m")
        stamps.append(ft.t)
    assert all(len([x for x in stamps if ts - 60 < x <= ts]) <= 12 for ts in stamps)


def test_the_limiter_is_per_model():
    ft = FakeTime()
    limiter = RateLimiter(2, 30.0, ft.clock, ft.sleep)
    for _ in range(2):
        limiter.acquire("a")
    limiter.acquire("b")  # a different model has its own window
    assert ft.slept == []


def test_the_limiter_gives_up_when_no_slot_opens_within_the_maximum_wait():
    ft = FakeTime()
    limiter = RateLimiter(1, 30.0, ft.clock, ft.sleep)
    limiter.acquire("m")
    with pytest.raises(LLMUnavailable, match="no request slot"):
        limiter.acquire("m")  # the slot opens in 60 s: more than the 30 s it may wait
    assert ft.slept == []  # it fails at once rather than sit out a wait it cannot finish
    ft.t += 35  # now the slot opens in 25 s, which is within the limit
    limiter.acquire("m")
    assert ft.slept == [pytest.approx(25)]


def test_the_client_takes_a_slot_before_every_send_including_retries():
    ft = FakeTime()
    limiter = RateLimiter(12, 30.0, ft.clock, ft.sleep)
    fake = FakeGenai([unavailable(), unavailable(), text(GOOD)])
    client = GeminiClient(
        genai_client=fake,
        model="m",
        fallback_model=None,
        sleep=lambda s: None,
        rate_limiter=limiter,
    )
    client.generate("s", "u", Out)
    assert len(limiter._hits["m"]) == 3 == len(fake.models.calls)


def test_a_client_with_an_injected_transport_is_not_rate_limited_by_default():
    assert GeminiClient(genai_client=FakeGenai([]), model="m")._limiter is None
    from grocery_planner.llm.ratelimit import DEFAULT_RATE_LIMITER

    assert DEFAULT_RATE_LIMITER.per_minute == 12 and DEFAULT_RATE_LIMITER.max_wait == 30.0


# ---- the budget inside a run -------------------------------------------------------------------


def run_with_budget(budget, script_fn=None, **kw):
    from tests.test_orchestrator import (
        REQUEST,
        factory_for,
        happy_script,
        make_runner,
        run,
    )

    def make(hooks):
        from grocery_planner.llm import FakeLLMClient

        return FakeLLMClient(
            (script_fn or happy_script)(),
            on_request=hooks.on_request,
            before_request=hooks.before_request,
        )

    runner, store = make_runner(llm_factory=make, budget=budget, **kw)
    assert factory_for
    return (runner, store, *run(runner, request=REQUEST))


def test_a_run_reserves_its_worst_case_then_uses_and_releases_it():
    budget = InMemoryDayBudget("fake-model", None, SMALL, Clock(), "k")
    runner, store, run_id, row = run_with_budget(budget)
    assert row.status == "completed" and row.estimated_requests == 9
    (u,) = budget.snapshot()
    assert (u.used, u.reserved) == (3, 0)  # three requests used; the unused 6 went back


def test_a_failed_run_still_returns_its_reservation():
    from grocery_planner.llm import SchemaValidationFailed
    from tests.test_orchestrator import failing_script

    budget = InMemoryDayBudget("fake-model", None, SMALL, Clock(), "k")
    _, _, _, row = run_with_budget(
        budget, failing_script(2, SchemaValidationFailed(["a", "b"], ["x"]))
    )
    assert row.status == "failed"
    (u,) = budget.snapshot()
    assert (u.used, u.reserved) == (2, 0)


def test_a_run_that_cannot_be_admitted_never_starts():
    budget = InMemoryDayBudget("fake-model", None, SMALL, Clock(), "k")
    budget._row(pacific_day(START), "fake-model").update(used=19)  # 19 + 9 > 20
    runner, store, run_id, row = run_with_budget(budget)
    assert row.status == "failed" and row.failure_reason == "day_budget"
    assert store.get_steps(run_id) == [] and row.ingredient_output is None
    assert "used up today's AI capacity" in row.user_message
    assert not budget.availability().available
    assert budget.snapshot()[0].reserved == 0


def test_a_busy_refusal_has_its_own_reason_and_message():
    budget = InMemoryDayBudget("fake-model", None, SMALL, Clock(), "k")
    budget.reserve("someone-else", 9)
    budget.reserve("another", 9)  # 18 reserved: a third run does not fit right now
    _, _, _, row = run_with_budget(budget)
    assert row.failure_reason == "busy" and "try again in a few minutes" in row.user_message
    assert budget.availability().available


def test_the_daily_limit_ends_an_in_flight_run_cleanly_and_keeps_its_work():
    cfg = GuardrailConfig(daily_limit=2, admission_limit=100)
    budget = InMemoryDayBudget("fake-model", None, cfg, Clock(), "k")
    runner, store, run_id, row = run_with_budget(budget)
    assert row.status == "failed" and row.failure_reason == "budget_exhausted"
    assert "ran out while this plan was running" in row.user_message
    assert [s.name for s in store.get_steps(run_id)] == ["ingredient", "price", "planner"]
    assert store.get_steps(run_id)[2].status == "failed"
    assert store.get_steps(run_id)[2].error == "BudgetExhausted"
    assert row.ingredient_output is not None and "ingredient list is below" in row.user_message
    assert budget.snapshot()[0].reserved == 0 and budget.snapshot()[0].used == 2


def test_sweeping_a_stale_run_returns_whatever_it_had_reserved():
    from tests.test_orchestrator import make_runner

    budget = InMemoryDayBudget("fake-model", None, SMALL, Clock(), "k")
    runner, store = make_runner(budget=budget)
    run_id = store.create_run({}, {}, 9)
    store.update_run(run_id, status="running")
    budget.reserve(run_id, 9)
    store.runs[run_id] = store.runs[run_id].model_copy(
        update={"updated_at": datetime.now(UTC) - timedelta(minutes=6)}
    )
    assert runner.sweep_stale() == [run_id]
    assert budget.snapshot()[0].reserved == 0


def test_the_cli_refuses_to_start_when_the_day_is_stopped(capsys, tmp_path):
    from grocery_planner.cli import main
    from tests.test_orchestrator import REQUEST, make_runner

    budget = InMemoryDayBudget("fake-model", None, SMALL, Clock(), "k")
    budget.quota_exhausted("fake-model", "per_day")
    runner, store = make_runner(budget=budget)
    path = tmp_path / "r.json"
    path.write_text(REQUEST.model_dump_json())
    assert main(["run", str(path)], lambda args, as_of: runner) == 3
    out = capsys.readouterr().out
    assert "used up today's AI capacity" in out and "Sydney time" in out
    assert "STOPPED (google_quota)" in out and store.runs == {}  # no run row was created


def test_the_cli_prints_the_counters_after_a_run(capsys, tmp_path):
    from grocery_planner.cli import main
    from tests.test_orchestrator import REQUEST, make_runner

    budget = InMemoryDayBudget("fake-model", "backup", SMALL, Clock(), "k")
    runner, _ = make_runner(budget=budget)
    path = tmp_path / "r.json"
    path.write_text(REQUEST.model_dump_json())
    assert main(["run", str(path)], lambda args, as_of: runner) == 0
    out = capsys.readouterr().out
    assert "BUDGET (requests, Pacific day)" in out
    assert "fake-model" in out and "used   3  reserved  0" in out and "admission limit 20" in out
    assert "backup" in out and "availability: available" in out
