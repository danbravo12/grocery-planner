"""The API through TestClient, with scripted model clients and the in-memory store."""

import json
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from grocery_planner.agents.price import MatchBatch
from grocery_planner.api import ApiSettings, ConfigError, create_app
from grocery_planner.api.app import VISITOR_COOKIE, Services
from grocery_planner.config import GuardrailConfig
from grocery_planner.domain import PlannerConfig
from grocery_planner.domain.ingredients import ExpansionBatch
from grocery_planner.guardrails import InMemoryDayBudget
from grocery_planner.llm import FakeLLMClient
from grocery_planner.orchestrator.events import events_from_state
from tests.test_orchestrator import (
    AS_OF,
    REQUEST,
    factory_for,
    happy_script,
    ing,
    ingredients,
    make_runner,
    planner,
    prices,
)

BODY = REQUEST.model_dump(mode="json")
FAST = dict(poll_seconds=0.01, heartbeat_seconds=60.0)


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    monkeypatch.setattr("grocery_planner.api.app.today_sydney", lambda: AS_OF)


class Gate:
    """Makes the fake model wait before its Nth call, so a test can hold a run mid-way."""

    def __init__(self, block_from: int = 1):
        self.event = threading.Event()
        self.block_from = block_from
        self.calls = 0
        self._lock = threading.Lock()

    def open(self):
        self.event.set()

    def factory(self, script_fn=happy_script):
        def make(hooks):
            fake = FakeLLM(script_fn(), hooks)
            for name in ("generate", "generate_with_tools"):
                setattr(fake, name, self._wrap(getattr(fake, name)))
            return fake

        return make

    def _wrap(self, inner):
        def call(*a, **k):
            with self._lock:
                self.calls += 1
                n = self.calls
            if n >= self.block_from:
                assert self.event.wait(15), "gate never opened"
            return inner(*a, **k)

        return call


class SmartFake(FakeLLMClient):
    """Answers by what is asked, not by order, so any number of runs can share the dish cache
    (a cached dish skips the ingredient call, which would shift a scripted list)."""

    def _next(self, system, user, output_type, tools=None):
        self.calls.append({"system": system, "user": user, "output_type": output_type})
        self._last = (system, user)
        if issubclass(output_type, ExpansionBatch):
            return ingredients(ing("bacon", 100), ing("eggs", 2, "each"))
        if issubclass(output_type, MatchBatch):
            return prices(
                ("i1", "col-eggs-01", "wol-eggs-01"), ("i2", "col-bacon-02", "wol-bacon-01")
            )
        return planner()


def FakeLLM(script, hooks):
    return SmartFake(script, on_request=hooks.on_request, before_request=hooks.before_request)


def build(script_fn=happy_script, settings=None, config=None, llm_factory=None, ip_secret="s3cret"):
    budget = InMemoryDayBudget(
        "fake-model", None, config=config or GuardrailConfig(), ip_secret=ip_secret
    )
    runner, store = make_runner(
        script_fn, budget=budget, llm_factory=llm_factory or factory_for(script_fn)
    )
    services = Services(runner=runner, budget=budget)
    app = create_app(settings or ApiSettings(**FAST), services)
    return app, services, store, budget


def parse_sse(text: str) -> list[dict]:
    out = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        entry: dict = {"comment": None, "event": None, "id": None, "data": None}
        for line in block.split("\n"):
            if line.startswith(":"):
                entry["comment"] = line[1:].strip()
            elif line.startswith("event:"):
                entry["event"] = line[6:].strip()
            elif line.startswith("id:"):
                entry["id"] = int(line[3:].strip())
            elif line.startswith("data:"):
                entry["data"] = json.loads(line[5:].strip())
        out.append(entry)
    return out


def stream_all(client, url, **kw) -> list[dict]:
    with client.stream("GET", url, **kw) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        return parse_sse("".join(r.iter_text()))


def stream_until(client, url, stop, **kw) -> list[dict]:
    """Read the stream until `stop(entry)` is true, then drop the connection (a closed tab)."""
    seen, buffer = [], ""
    with client.stream("GET", url, **kw) as r:
        for chunk in r.iter_text():
            buffer += chunk
            while "\n\n" in buffer:
                block, buffer = buffer.split("\n\n", 1)
                entry = parse_sse(block)[0] if block.strip() else None
                if entry:
                    seen.append(entry)
                    if stop(entry):
                        return seen
    return seen


def wait_for(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    raise AssertionError("timed out waiting")


@contextmanager
def live_client(app):
    """A real server on a free port and a real HTTP client. TestClient buffers a whole response,
    so it cannot follow a stream while a run is still going; these tests need to."""
    import uvicorn

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    wait_for(lambda: server.started)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=20) as client:
            yield client
    finally:
        server.should_exit = True
        thread.join(10)


def post(client, body=None, **kw):
    return client.post("/api/runs", json=BODY if body is None else body, **kw)


def done(store, run_id):
    return store.runs[run_id].status in ("completed", "stopped", "failed")


# ---- happy path ------------------------------------------------------------------------------


def test_post_returns_202_then_events_stream_to_run_completed_and_get_matches():
    app, _, store, _ = build()
    with TestClient(app) as client:
        r = post(client)
        assert r.status_code == 202
        body = r.json()
        run_id = body["run_id"]
        assert body["events_url"] == f"/api/runs/{run_id}/events"
        assert body["run_url"].endswith(f"/runs/{run_id}")
        entries = [e for e in stream_all(client, body["events_url"]) if e["event"]]
        types = [e["event"] for e in entries]
        assert types == ["step_started", "step_completed"] * 3 + ["run_completed"]
        assert [e["id"] for e in entries] == list(range(1, len(entries) + 1))
        view = client.get(f"/api/runs/{run_id}").json()
        assert view["status"] == "completed" and view["id"] == run_id
        assert entries[-1]["data"]["summary"] == view["recommendation"]["summary_line"]
        assert view["recommendation"]["recommendation"] == "no_meaningful_difference"
        assert {i["name"] for i in view["ingredient_list"]["items"]} == {"eggs", "bacon"}
        assert [c["name"] for c in view["comparison"]] == ["eggs", "bacon"]
        assert set(view["comparison"][0]["stores"]) == {"coles", "woolworths"}
        assert view["request"]["meals"][0]["meal_name"] == "Brekky"
        assert view["totals"]["requests"] == 3 and view["totals"]["models"] == ["fake-model"]
        assert view["totals"]["request_budget"] == 9  # the reservation: worst case for a run
        assert view["totals"]["would_cost_usd"] is not None
        assert "condition" in view["recommendation"]["rule_verdict"]
        steps = client.get(f"/api/runs/{run_id}/steps").json()["steps"]
        assert [s["name"] for s in steps] == ["ingredient", "price", "planner"]
        assert all(s["status"] == "completed" and s["model"] == "fake-model" for s in steps)
        assert steps[0]["output"]["items"] == 2 and steps[2]["output"]["confidence"]
        assert steps[0]["output"]["cache_hits"] == 0 and steps[0]["output"]["cache_misses"] == 1
        assert steps[1]["output"]["downgrades"] == [] and steps[1]["output"]["rounds"] == 1
    assert store.runs[run_id].status == "completed"


def test_visitor_cookie_is_set_once_with_the_right_attributes():
    app, _, _, _ = build()
    with TestClient(app) as client:
        first = post(client)
        cookie = first.headers["set-cookie"]
        assert f"{VISITOR_COOKIE}=" in cookie and "HttpOnly" in cookie
        assert "SameSite=lax" in cookie and "Max-Age=31536000" in cookie
        assert "Secure" not in cookie  # dev
        second = post(client)
        assert "set-cookie" not in second.headers  # the browser already has one


def test_cookie_is_secure_in_prod():
    settings = ApiSettings(app_env="prod", ip_hash_secret="x", sample_run_id="s", **FAST)
    app, _, _, _ = build(settings=settings)
    with TestClient(app, base_url="https://testserver") as client:
        assert "Secure" in post(client).headers["set-cookie"]


# ---- the stream: replay, heartbeat, queued -----------------------------------------------------


def test_sse_replays_exactly_the_missing_events_after_a_reconnect():
    gate = Gate(block_from=2)  # the ingredient step finishes, then the model stalls
    app, _, store, _ = build(llm_factory=gate.factory())
    with live_client(app) as client:
        run_id = post(client).json()["run_id"]
        first = stream_until(
            client, f"/api/runs/{run_id}/events", lambda e: e["event"] == "step_completed"
        )  # then the client goes away
        seen = [e for e in first if e["event"]]
        assert [e["id"] for e in seen] == [1, 2]
        gate.open()
        wait_for(lambda: done(store, run_id))
        resumed = [
            e for e in stream_all(client, f"/api/runs/{run_id}/events",
                                  headers={"Last-Event-ID": "2"}) if e["event"]
        ]  # fmt: skip
        full = events_from_state(store.runs[run_id], store.steps[run_id])
        assert [e["id"] for e in resumed] == [e.seq for e in full if e.seq > 2]
        assert [e["event"] for e in seen + resumed] == [e.type for e in full]
        # ?after= does the same
        again = stream_all(client, f"/api/runs/{run_id}/events?after={len(full) - 1}")
        assert [e["event"] for e in again if e["event"]] == ["run_completed"]
        # nothing missing: closes straight away with no events
        assert not [e for e in stream_all(client, f"/api/runs/{run_id}/events?after={len(full)}")
                    if e["event"]]  # fmt: skip


def test_heartbeat_comments_keep_a_slow_stream_alive():
    gate = Gate(block_from=1)
    settings = ApiSettings(poll_seconds=0.01, heartbeat_seconds=0.05)
    app, _, store, _ = build(settings=settings, llm_factory=gate.factory())
    with live_client(app) as client:
        run_id = post(client).json()["run_id"]
        seen = stream_until(
            client, f"/api/runs/{run_id}/events", lambda e: e["comment"] == "heartbeat"
        )
        assert any(e["comment"] == "heartbeat" for e in seen)
        gate.open()
        wait_for(lambda: done(store, run_id))


def test_run_survives_a_disconnected_client():
    gate = Gate(block_from=2)
    app, _, store, _ = build(llm_factory=gate.factory())
    with live_client(app) as client:
        run_id = post(client).json()["run_id"]
        stream_until(client, f"/api/runs/{run_id}/events", lambda e: e["event"] == "step_started")
        gate.open()
        wait_for(lambda: done(store, run_id))
    assert store.runs[run_id].status == "completed"


def test_queue_reports_positions_and_the_13th_request_is_busy():
    gate = Gate(block_from=1)
    app, services, store, _ = build(llm_factory=gate.factory())
    with live_client(app) as client:
        ids = []
        for n in range(12):  # 2 running + 10 waiting
            client.cookies.clear()
            r = post(client)
            assert r.status_code == 202, (n, r.text)
            ids.append(r.json()["run_id"])
            if n == 1:
                wait_for(lambda: services.queue.counts()[0] == 2)  # both workers have a run
        client.cookies.clear()
        busy = post(client)
        assert busy.status_code == 503 and busy.json()["code"] == "busy"
        assert busy.headers["retry-after"] == "30"
        assert len(store.runs) == 12  # the refused request left no run behind
        # positions: ids[0], ids[1] run; ids[2] is next; ids[11] is last
        for run_id, position in ((ids[2], 1), (ids[11], 10)):
            seen = stream_until(
                client, f"/api/runs/{run_id}/events", lambda e: e["event"] == "queued"
            )
            queued = next(e for e in seen if e["event"] == "queued")
            assert queued["data"]["position"] == position and queued["id"] is None
        assert services.queue.position(ids[0]) == 0
        gate.open()
        wait_for(lambda: all(done(store, i) for i in ids), timeout=30)
        assert {store.runs[i].status for i in ids} == {"completed"}
        assert post(client).status_code == 202  # room again


def test_a_full_queue_at_enqueue_time_releases_the_reservation():
    gate = Gate(block_from=1)
    app, services, store, budget = build(llm_factory=gate.factory())
    with TestClient(app) as client:
        for _ in range(12):
            client.cookies.clear()
            assert post(client).status_code == 202
        wait_for(lambda: services.queue.counts() == (2, 10))
        reserved = budget._row(budget._today(), "fake-model")["reserved"]
        services.queue.has_room = lambda: True  # the race: room was seen, then it filled
        client.cookies.clear()
        r = post(client)
        assert r.status_code == 503 and r.json()["code"] == "busy"
        assert budget._row(budget._today(), "fake-model")["reserved"] == reserved
        assert len(store.runs) == 12
        gate.open()
        wait_for(lambda: all(done(store, i) for i in list(store.runs)), timeout=30)


# ---- the order of checks -----------------------------------------------------------------------


def visitor_counts(budget):
    return {k: v for k, v in budget.counters.items() if k[1] == "visitor"}


def test_busy_is_refused_before_admit_and_leaves_the_visitor_count_alone():
    gate = Gate(block_from=1)
    config = GuardrailConfig(admission_limit=9, daily_limit=12)  # room for exactly one run
    app, _, store, budget = build(config=config, llm_factory=gate.factory())
    with TestClient(app) as client:
        assert post(client).status_code == 202
        before = dict(budget.counters)
        client.cookies.clear()
        r = post(client)
        assert r.status_code == 503 and r.json()["code"] == "busy"
        assert budget.counters == before  # admit() never ran for the refused request
        assert len(store.runs) == 1
        gate.open()
        wait_for(lambda: done(store, next(iter(store.runs))))


def test_day_budget_is_refused_before_admit():
    config = GuardrailConfig(admission_limit=8, daily_limit=12)  # a run needs 9: never fits
    app, _, store, budget = build(config=config)
    with TestClient(app) as client:
        r = post(client)
        assert r.status_code == 503 and r.json()["code"] == "day_budget"
        assert budget.counters == {} and store.runs == {}
        assert client.get("/api/availability").json()["available"] is False  # the day is stopped


def test_visitor_limit_refusal_releases_the_reservation():
    gate = Gate(block_from=1)
    app, _, store, budget = build(
        config=GuardrailConfig(visitor_runs_per_day=1), llm_factory=gate.factory()
    )
    with TestClient(app) as client:
        assert post(client).status_code == 202
        reserved = budget._row(budget._today(), "fake-model")["reserved"]
        assert reserved == 9
        r = post(client)  # same cookie
        assert r.status_code == 429 and r.json()["code"] == "visitor_limit"
        assert budget._row(budget._today(), "fake-model")["reserved"] == reserved  # released
        assert len(store.runs) == 1
        gate.open()
        wait_for(lambda: done(store, next(iter(store.runs))))


def test_ip_limit_refusal_releases_the_reservation_and_undoes_the_visitor_count():
    gate = Gate(block_from=1)
    app, _, store, budget = build(
        config=GuardrailConfig(ip_runs_per_day=1), llm_factory=gate.factory()
    )
    with TestClient(app) as client:
        assert post(client).status_code == 202
        client.cookies.clear()
        r = post(client)
        assert r.status_code == 429 and r.json()["code"] == "ip_limit"
        assert budget._row(budget._today(), "fake-model")["reserved"] == 9
        assert len(visitor_counts(budget)) == 1  # the second visitor's count was not kept
        gate.open()
        wait_for(lambda: done(store, next(iter(store.runs))))


def test_unavailable_when_the_day_is_stopped():
    app, _, _, budget = build()
    budget.quota_exhausted("fake-model", "per_day")
    with TestClient(app) as client:
        r = post(client)
        assert r.status_code == 503 and r.json()["code"] == "unavailable"
        body = r.json()
        assert (
            body["reset_at_utc"] and body["reset_at_sydney"] and int(r.headers["retry-after"]) > 0
        )
        assert budget.counters == {}
        a = client.get("/api/availability").json()
        assert a["available"] is False and a["reason"] == "google_quota"
        assert set(a) == {"available", "reason", "message", "reset_at_utc", "reset_at_sydney",
                          "sample_run_id"}  # fmt: skip


def test_availability_when_open_includes_the_sample_run():
    app, _, _, _ = build(settings=ApiSettings(sample_run_id="abc", **FAST))
    with TestClient(app) as client:
        a = client.get("/api/availability").json()
        assert a["available"] is True and a["reason"] is None and a["sample_run_id"] == "abc"


# ---- errors ------------------------------------------------------------------------------------


def test_validation_errors_are_422_with_field_messages():
    app, _, store, _ = build()
    bad = {**BODY, "meals": [{**BODY["meals"][0], "people": 99, "mains": []}], "extra": 1}
    with TestClient(app) as client:
        r = post(client, bad)
        assert r.status_code == 422 and r.json()["code"] == "validation_error"
        fields = {f["field"]: f["message"] for f in r.json()["fields"]}
        assert "meals.0.people" in fields and "meals.0.mains" in fields and "extra" in fields
        assert "less than or equal to 12" in fields["meals.0.people"]
        too_many = {"meals": [{**BODY["meals"][0], "mains": [f"dish {n}" for n in range(4)]}] * 6}
        r = post(client, too_many)
        assert r.status_code == 422 and "at most 20 dishes" in r.json()["fields"][0]["message"]
        r = post(client, {**BODY, "parameters": {"savings_threshold_dollars": "1"}})
        assert r.status_code == 422 and r.json()["fields"][0]["field"].startswith("parameters")
        assert store.runs == {}
        r = client.post(
            "/api/runs", content="{not json", headers={"content-type": "application/json"}
        )
        assert r.status_code == 400 and r.json()["code"] == "invalid_json"


def test_parameters_are_accepted_and_stored_with_the_run():
    app, _, store, _ = build()
    with TestClient(app) as client:
        r = post(client, {**BODY, "parameters": {"savings_threshold_dollars": "10"}})
        run_id = r.json()["run_id"]
        wait_for(lambda: done(store, run_id))
        view = client.get(f"/api/runs/{run_id}").json()
        assert view["parameters"]["config"]["savings_threshold_dollars"] == "10"


def test_body_too_large_is_413():
    app, _, store, _ = build()
    huge = {**BODY, "padding": "x" * 40_000}
    with TestClient(app) as client:
        r = post(client, huge)
        assert r.status_code == 413 and r.json()["code"] == "body_too_large"

        # no Content-Length (chunked): still refused
        def chunks():
            for _ in range(10):
                yield b" " * 5000

        r = client.post("/api/runs", content=chunks(), headers={"content-type": "application/json"})
        assert r.status_code == 413
        assert store.runs == {}


def test_unknown_and_malformed_ids_are_404_and_errors_never_show_a_trace():
    app, services, _, _ = build()
    with TestClient(app, raise_server_exceptions=False) as client:
        for path in ("/api/runs/00000000-0000-4000-8000-000000000000", "/api/runs/nope",
                     "/api/runs/nope/steps", "/api/runs/nope/events", "/nothing"):  # fmt: skip
            r = client.get(path)
            assert r.status_code == 404 and set(r.json()) == {"code", "message"}, path
        assert client.delete("/api/runs/x").json()["code"] == "method_not_allowed"
        services.budget.availability = lambda *a: (_ for _ in ()).throw(
            RuntimeError("secret detail")
        )
        r = client.get("/api/availability")
        assert r.status_code == 500 and r.json() == {
            "code": "internal", "message": "Something went wrong on our side.",
        }  # fmt: skip


# ---- what is public ----------------------------------------------------------------------------

FORBIDDEN_KEYS = {
    "visitor", "visitor_id", "ip", "ip_hash", "reserved", "reserved_remaining", "reservation_day",
    "langfuse_trace_id", "langfuse", "trace_id", "cost_usd", "estimated_requests", "updated_at",
    "thinking_tokens_raw", "input_tokens",
}  # fmt: skip


def all_keys(value, found=None):
    found = found if found is not None else set()
    if isinstance(value, dict):
        for k, v in value.items():
            found.add(k)
            all_keys(v, found)
    elif isinstance(value, list):
        for v in value:
            all_keys(v, found)
    return found


def test_public_views_leak_nothing_private():
    app, _, store, budget = build()
    with TestClient(app) as client:
        r = post(client, headers={"x-forwarded-for": "203.0.113.9"})
        run_id = r.json()["run_id"]
        visitor = client.cookies.get(VISITOR_COOKIE)
        wait_for(lambda: done(store, run_id))
        store.update_run(run_id, langfuse_trace_id="trace-123")
        views = [
            client.get(f"/api/runs/{run_id}").json(),
            client.get(f"/api/runs/{run_id}/steps").json(),
            client.get("/api/availability").json(),
        ]
        text = json.dumps(views)
        for view in views:
            assert not all_keys(view) & FORBIDDEN_KEYS, all_keys(view) & FORBIDDEN_KEYS
        ip_hash = budget._ips.hash("testclient", budget._today())
        for secret in (visitor, ip_hash, "trace-123", "203.0.113.9", "testclient", "s3cret"):
            assert secret not in text
        assert "stack" not in text.lower() and "Traceback" not in text
        # the stream carries no private fields either
        stream = "".join(
            json.dumps(e["data"]) for e in stream_all(client, f"/api/runs/{run_id}/events")
            if e["data"]
        )  # fmt: skip
        assert visitor not in stream and "trace-123" not in stream


def test_a_failed_run_shows_a_class_name_not_the_raw_error():
    from grocery_planner.llm import SchemaValidationFailed

    def script():
        return [SchemaValidationFailed(["{raw model output with secrets}"], ["bad.field: nope"])]

    app, _, store, _ = build(script_fn=script)
    with TestClient(app) as client:
        run_id = post(client).json()["run_id"]
        wait_for(lambda: done(store, run_id))
        view = client.get(f"/api/runs/{run_id}").json()
        steps = client.get(f"/api/runs/{run_id}/steps").json()["steps"]
        events = stream_all(client, f"/api/runs/{run_id}/events")
    assert view["status"] == "failed" and view["failure_reason"] == "schema_validation"
    assert view["message"] and steps[0]["error"] == "SchemaValidationFailed"
    blob = json.dumps([view, steps, events])
    assert "raw model output" not in blob and "bad.field" not in blob
    assert [e["event"] for e in events if e["event"]][-1] == "run_failed"


# ---- startup rules, CORS, docs -----------------------------------------------------------------


def test_prod_refuses_to_start_without_its_secrets(monkeypatch):
    with pytest.raises(ConfigError, match="IP_HASH_SECRET and SAMPLE_RUN_ID"):
        create_app(ApiSettings(app_env="prod"))
    with pytest.raises(ConfigError, match="SAMPLE_RUN_ID"):
        create_app(ApiSettings(app_env="prod", ip_hash_secret="x"))
    with pytest.raises(ConfigError, match="IP_HASH_SECRET"):
        create_app(ApiSettings(app_env="prod", sample_run_id="x"))
    create_app(ApiSettings(app_env="prod", ip_hash_secret="x", sample_run_id="y"))
    create_app(ApiSettings(app_env="dev"))  # dev needs neither
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("IP_HASH_SECRET", "")
    monkeypatch.setenv("SAMPLE_RUN_ID", "")
    with pytest.raises(ConfigError):
        ApiSettings.from_env()
    monkeypatch.setenv("IP_HASH_SECRET", "x")
    monkeypatch.setenv("SAMPLE_RUN_ID", "y")
    monkeypatch.setenv("FRONTEND_ORIGINS", "https://a.example/, https://b.example")
    monkeypatch.setenv("TRUSTED_PROXY", "true")
    s = ApiSettings.from_env()
    assert s.is_prod and s.trusted_proxy and s.frontend_origins == (
        "https://a.example", "https://b.example",
    )  # fmt: skip


def test_cors_allows_only_the_configured_origins():
    settings = ApiSettings(frontend_origins=("https://app.example",), **FAST)
    app, _, _, _ = build(settings=settings)
    with TestClient(app) as client:
        ok = client.options("/api/runs", headers={
            "origin": "https://app.example", "access-control-request-method": "POST",
            "access-control-request-headers": "content-type"})  # fmt: skip
        assert ok.status_code == 200
        assert ok.headers["access-control-allow-origin"] == "https://app.example"
        assert ok.headers["access-control-allow-credentials"] == "true"
        bad = client.options("/api/runs", headers={
            "origin": "https://evil.example", "access-control-request-method": "POST"})  # fmt: skip
        assert bad.status_code == 400 and "access-control-allow-origin" not in bad.headers
        plain = client.get("/api/availability", headers={"origin": "https://evil.example"})
        assert "access-control-allow-origin" not in plain.headers
        got = client.get("/api/availability", headers={"origin": "https://app.example"})
        assert got.headers["access-control-allow-origin"] == "https://app.example"


def test_no_origin_is_allowed_by_default():
    app, _, _, _ = build()
    with TestClient(app) as client:
        r = client.get("/api/availability", headers={"origin": "https://app.example"})
        assert "access-control-allow-origin" not in r.headers


def test_docs_only_exist_in_dev():
    dev, _, _, _ = build(settings=ApiSettings(app_env="dev", **FAST))
    prod, _, _, _ = build(
        settings=ApiSettings(app_env="prod", ip_hash_secret="x", sample_run_id="y", **FAST)
    )
    with TestClient(dev) as client:
        assert client.get("/docs").status_code == 200
        assert client.get("/openapi.json").status_code == 200
    with TestClient(prod) as client:
        for path in ("/docs", "/openapi.json", "/redoc"):
            r = client.get(path)
            assert r.status_code == 404 and r.json()["code"] == "not_found"


# ---- the client address ------------------------------------------------------------------------


def ip_keys(budget):
    return {k[2] for k in budget.counters if k[1] == "ip"}


def test_forwarded_for_is_ignored_unless_the_proxy_is_trusted():
    app, _, store, budget = build()
    with TestClient(app) as client:
        run_id = post(client, headers={"x-forwarded-for": "1.1.1.1, 2.2.2.2"}).json()["run_id"]
        wait_for(lambda: done(store, run_id))
        day = budget._today()
        assert ip_keys(budget) == {budget._ips.hash("testclient", day)}
    app, _, store, budget = build(settings=ApiSettings(trusted_proxy=True, **FAST))
    with TestClient(app) as client:
        run_id = post(client, headers={"x-forwarded-for": "1.1.1.1, 2.2.2.2"}).json()["run_id"]
        wait_for(lambda: done(store, run_id))
        # the last entry is the one our own proxy added; earlier ones are the client's claim
        assert ip_keys(budget) == {budget._ips.hash("2.2.2.2", budget._today())}
        post(client)  # no header: falls back to the socket address
        wait_for(lambda: all(done(store, i) for i in store.runs))
        assert budget._ips.hash("testclient", budget._today()) in ip_keys(budget)


def test_a_malformed_cookie_is_replaced():
    app, _, _, budget = build()
    with TestClient(app) as client:
        client.cookies.set(VISITOR_COOKIE, "short")
        r = post(client)
        assert r.status_code == 202 and VISITOR_COOKIE in r.headers["set-cookie"]
        assert "short" not in {k[2] for k in budget.counters}


# ---- maintenance -------------------------------------------------------------------------------


def test_startup_sweeps_stale_runs_and_prunes_old_counters():
    app, services, store, budget = build()
    stale = services.runner.create_run(REQUEST, PlannerConfig(), AS_OF)
    store.update_run(stale, status="running")
    store.runs[stale] = store.runs[stale].model_copy(
        update={"updated_at": datetime(2020, 1, 1, tzinfo=UTC)}
    )
    old_day = budget._today().replace(year=2020)
    budget.counters[(old_day, "visitor", "old")] = 3
    with TestClient(app):  # the lifespan runs both jobs once on startup
        wait_for(lambda: store.runs[stale].status == "failed")
        wait_for(lambda: (old_day, "visitor", "old") not in budget.counters)
    assert store.runs[stale].failure_reason == "stale"


def test_prune_keeps_the_last_week():
    budget = InMemoryDayBudget("m", None, ip_secret="x")
    today = budget._today()
    from datetime import timedelta

    for age in (0, 6, 7, 8, 30):
        budget.counters[(today - timedelta(days=age), "ip", f"k{age}")] = 1
        budget._rows[(today - timedelta(days=age), "m")] = {
            "used": 1, "reserved": 0, "stopped": False, "reason": None,
        }  # fmt: skip
    assert budget.prune(7) == 4  # 8 and 30 days old, in both tables
    assert {k[2] for k in budget.counters} == {"k0", "k6", "k7"}


# ---- queued runs that a restart orphaned -------------------------------------------------------


def old_queued_run(services, budget, age_seconds):
    """A run row left 'queued' by a previous process, with its budget still reserved."""
    run_id = services.runner.create_run(REQUEST, PlannerConfig(), AS_OF)
    budget.reserve(run_id, 9)
    store = services.runner.store
    store.runs[run_id] = store.runs[run_id].model_copy(
        update={"created_at": datetime.now(UTC) - timedelta(seconds=age_seconds)}
    )
    return run_id


def test_startup_fails_queued_runs_the_new_process_does_not_hold():
    app, services, store, budget = build()
    orphan = old_queued_run(services, budget, 120)
    fresh = old_queued_run(services, budget, 5)  # row written just before it is queued: left alone
    assert budget._row(budget._today(), "fake-model")["reserved"] == 18
    with TestClient(app) as client:
        wait_for(lambda: store.runs[orphan].status == "failed")
        run = store.runs[orphan]
        assert run.failure_reason == "restarted"
        assert run.user_message == "The server restarted before your run started. Please try again."
        assert run.completed_at is not None
        assert store.runs[fresh].status == "queued"
        assert budget._row(budget._today(), "fake-model")["reserved"] == 9  # orphan released
        view = client.get(f"/api/runs/{orphan}").json()
        assert view["status"] == "failed" and view["failure_reason"] == "restarted"
        events = [e for e in stream_all(client, f"/api/runs/{orphan}/events") if e["event"]]
        assert [e["event"] for e in events] == ["run_failed"]
        assert events[0]["data"]["data"]["reason"] == "restarted"


def test_the_sweep_leaves_queued_runs_the_process_holds_and_fails_ones_waiting_too_long():
    gate = Gate(block_from=1)
    app, services, store, budget = build(llm_factory=gate.factory())
    with TestClient(app) as client:
        ids = []
        for _ in range(3):  # 2 running, 1 waiting
            client.cookies.clear()
            ids.append(post(client).json()["run_id"])
        wait_for(lambda: services.queue.counts() == (2, 1))
        waiting = ids[2]
        # long enough to be past the grace period, but the queue holds it: not swept
        store.runs[waiting] = store.runs[waiting].model_copy(
            update={"created_at": datetime.now(UTC) - timedelta(minutes=5)}
        )
        assert services.runner.sweep_queued(lambda r: services.queue.position(r) is not None) == []
        # older than ten minutes: failed even though it is queued here
        store.runs[waiting] = store.runs[waiting].model_copy(
            update={"created_at": datetime.now(UTC) - timedelta(minutes=11)}
        )
        swept = services.runner.sweep_queued(lambda r: services.queue.position(r) is not None)
        assert swept == [waiting] and store.runs[waiting].failure_reason == "restarted"
        gate.open()
        wait_for(lambda: done(store, ids[0]) and done(store, ids[1]))
        wait_for(lambda: services.queue.counts() == (0, 0))
        # the worker that later pulled the swept run did not start it
        assert store.runs[waiting].status == "failed" and store.steps[waiting] == []
        assert budget._row(budget._today(), "fake-model")["reserved"] == 0


# ---- the built frontend, same origin -----------------------------------------------------------


def test_the_built_frontend_is_served_with_a_fallback_for_client_side_routes(tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>app</html>")
    (dist / "assets" / "app-abc123.js").write_text("console.log(1)")
    (dist / "favicon.svg").write_text("<svg/>")
    (tmp_path / "secret.txt").write_text("outside the build")
    app, _, _, _ = build(settings=ApiSettings(frontend_dist=dist, **FAST))
    with TestClient(app) as client:
        assert client.get("/").text == "<html>app</html>"
        runs = client.get("/runs/3a1f9a52-0000-4000-8000-000000000000")  # a client-side route
        assert runs.status_code == 200 and runs.text == "<html>app</html>"
        assert runs.headers["cache-control"] == "no-cache"
        asset = client.get("/assets/app-abc123.js")
        assert asset.text == "console.log(1)" and "immutable" in asset.headers["cache-control"]
        assert client.get("/favicon.svg").text == "<svg/>"
        # the API still answers as the API, and unknown API paths are JSON 404s, not the app
        assert client.get("/health").json() == {"status": "ok"}
        r = client.get("/api/nothing")
        assert r.status_code == 404 and r.json()["code"] == "not_found"
        # nothing outside the build directory
        assert client.get("/..%2Fsecret.txt").text == "<html>app</html>"
        assert "outside" not in client.get("/%2e%2e/secret.txt").text


def test_without_a_build_unknown_paths_are_json_404s():
    app, _, _, _ = build()
    with TestClient(app) as client:
        r = client.get("/runs/abc")
        assert r.status_code == 404 and r.json()["code"] == "not_found"


def test_the_openapi_contract_types_every_public_response():
    app, _, _, _ = build(settings=ApiSettings(app_env="dev", **FAST))
    with TestClient(app) as client:
        spec = client.get("/openapi.json").json()
    schemas = spec["components"]["schemas"]
    for name in ("RunView", "Step", "Recommendation", "Condition", "Pick", "CreateRun",
                 "Availability", "ErrorBody", "ValidationErrorBody", "RunAccepted"):  # fmt: skip
        assert name in schemas, name
    post = spec["paths"]["/api/runs"]["post"]
    assert post["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "CreateRun"
    )
    assert set(post["responses"]) >= {"202", "400", "413", "422", "429", "503"}
    assert spec["paths"]["/api/runs/{run_id}"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["$ref"].endswith("RunView")
    forbidden = {"visitor", "ip", "reserved", "langfuse_trace_id", "cost_usd"}
    assert not forbidden & all_keys(schemas["RunView"]) & {"visitor", "ip"}
    for name, schema in schemas.items():
        assert not forbidden & set(schema.get("properties", {})), name
