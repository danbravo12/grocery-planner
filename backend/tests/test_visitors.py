"""The usage counter: unique browsers and runs. A browser is the HMAC of its cookie id; an admitted
run counts, a refused one never does; the owner's browser is left out; no hash or id leaves the
server in a response, and the stats are for the scheduler's service account only."""

import hashlib
import hmac
import json
import logging
import secrets
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from grocery_planner.api import ApiSettings, create_app
from grocery_planner.api.app import OWNER_COOKIE, VISITOR_COOKIE, owner_cookie_value
from grocery_planner.config import GuardrailConfig
from grocery_planner.db.visitors import SYDNEY, InMemoryVisitors, visitor_hash
from tests.test_api import FAST, SmartFake, build, done, post, wait_for
from tests.test_daily import AUDIENCE, SCHEDULER, verifier

SECRET = "visitor-secret-for-tests"
OWNER_TOKEN = "owner-token-for-tests"
AUTH = {"Authorization": "Bearer good"}


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    from tests.test_orchestrator import AS_OF

    monkeypatch.setattr("grocery_planner.api.app.today_sydney", lambda: AS_OF)


def smart(hooks):
    """Answers by what is asked, so any number of runs can share the app."""
    return SmartFake([], on_request=hooks.on_request, before_request=hooks.before_request)


def counting(config=None, **kw):
    """An app with the counter on. Returns (app, services, store, visitors)."""
    base = dict(
        visitor_hash_secret=SECRET, owner_token=OWNER_TOKEN,
        daily_audience=AUDIENCE, daily_sa_email=SCHEDULER, **FAST,
    )  # fmt: skip
    app, services, store, _ = build(settings=ApiSettings(**{**base, **kw}), config=config,
                                    llm_factory=smart)  # fmt: skip
    services.verify_token, _ = verifier()
    services.visitors = InMemoryVisitors(store)
    return app, services, store, services.visitors


def finish(store):
    wait_for(lambda: all(done(store, r) for r in store.runs))


def cookie_of(client) -> str:
    return client.cookies.get(VISITOR_COOKIE)


# ---- counting ----------------------------------------------------------------------------------


def test_visitor_hash_is_an_hmac_of_the_cookie_id_under_the_secret():
    h = visitor_hash(SECRET, "abc")
    assert h == hmac.new(SECRET.encode(), b"abc", hashlib.sha256).hexdigest()
    assert h != hashlib.sha256(b"abc").hexdigest() and "abc" not in h
    assert h != visitor_hash("another-secret", "abc") and h == visitor_hash(SECRET, "abc")


def test_each_admitted_run_updates_the_one_row_of_that_browser():
    app, _, store, visitors = counting()
    with TestClient(app) as client:
        assert post(client).status_code == 202
        first = dict(visitors.rows[visitor_hash(SECRET, cookie_of(client))])
        assert first["run_count"] == 1 and first["is_owner"] is False
        assert post(client).status_code == 202
        finish(store)
    assert list(visitors.rows) == [visitor_hash(SECRET, cookie_of(client))]
    row = visitors.rows[visitor_hash(SECRET, cookie_of(client))]
    assert row["run_count"] == 2 and row["last_seen"] >= first["last_seen"]
    assert row["first_seen"] == first["first_seen"]


def test_a_second_browser_is_a_second_row():
    app, _, store, visitors = counting()
    with TestClient(app) as a, TestClient(app) as b:
        post(a), post(b)
        finish(store)
    assert len(visitors.rows) == 2


def test_a_refused_request_does_not_touch_the_counter():
    app, _, store, visitors = counting(config=GuardrailConfig(visitor_runs_per_day=1))
    with TestClient(app) as client:
        assert post(client).status_code == 202
        before = {k: dict(v) for k, v in visitors.rows.items()}
        refused = post(client)  # over the visitor's daily limit
        assert refused.status_code == 429 and refused.json()["code"] == "visitor_limit"
        assert post(client, body={"meals": []}).status_code == 422  # invalid body
        finish(store)
    assert visitors.rows == before


def test_an_ip_limit_refusal_adds_no_visitor():
    app, _, store, visitors = counting(config=GuardrailConfig(ip_runs_per_day=1))
    with TestClient(app) as first, TestClient(app) as second:  # the same test address
        assert post(first).status_code == 202
        assert post(second).status_code == 429
        finish(store)
    assert list(visitors.rows) == [visitor_hash(SECRET, cookie_of(first))]


def test_a_run_refused_because_the_queue_is_full_is_not_counted(monkeypatch):
    app, services, store, visitors = counting()
    monkeypatch.setattr(services.queue, "try_enqueue", lambda run_id: False)
    with TestClient(app) as client:
        r = post(client)
    assert r.status_code == 503 and r.json()["code"] == "busy"
    assert visitors.rows == {} and store.runs == {}


def test_nothing_is_counted_without_a_secret_or_a_counter():
    no_secret, _, store2, visitors2 = counting(visitor_hash_secret=None)
    with TestClient(no_secret) as client:
        post(client)
        finish(store2)
    assert visitors2.rows == {}
    plain, services, store3, _ = counting()
    services.visitors = None
    with TestClient(plain) as client:
        assert post(client).status_code == 202  # the run is unaffected
        finish(store3)


def test_a_failing_counter_never_fails_the_run(caplog):
    app, services, store, _ = counting()

    class Broken:
        def record_run(self, visitor_hash, owner):
            raise RuntimeError("database down")

    services.visitors = Broken()
    with TestClient(app) as client, caplog.at_level(logging.ERROR):
        assert post(client).status_code == 202
        finish(store)
    assert "could not count a visitor" in caplog.text
    assert SECRET not in caplog.text and cookie_of(client) not in caplog.text


# ---- the owner -----------------------------------------------------------------------------------


def test_owner_endpoint_checks_the_token_and_sets_a_long_lived_cookie():
    app, *_ = counting()
    with TestClient(app) as client:
        assert client.get("/internal/owner").status_code == 403
        assert client.get("/internal/owner", params={"token": "wrong"}).status_code == 403
        assert OWNER_COOKIE not in client.cookies
        r = client.get("/internal/owner", params={"token": OWNER_TOKEN})
        assert r.status_code == 204 and r.content == b""
        set_cookie = r.headers["set-cookie"]
        assert "HttpOnly" in set_cookie and "SameSite=lax" in set_cookie
        assert "Max-Age=34560000" in set_cookie  # 400 days
        assert OWNER_TOKEN not in set_cookie  # the cookie holds a digest, never the token
        assert client.cookies[OWNER_COOKIE] == owner_cookie_value(OWNER_TOKEN)


def test_owner_cookie_is_secure_in_prod():
    app, *_ = counting(app_env="prod", ip_hash_secret="x", sample_run_id="s")
    with TestClient(app, base_url="https://testserver") as client:
        r = client.get("/internal/owner", params={"token": OWNER_TOKEN})
    assert "Secure" in r.headers["set-cookie"]


def test_owner_endpoint_is_closed_without_a_configured_token():
    app, *_ = counting(owner_token=None)
    with TestClient(app) as client:
        assert client.get("/internal/owner", params={"token": ""}).status_code == 503
        assert client.get("/internal/owner", params={"token": "x"}).status_code == 503
    assert "/internal/owner" not in create_app(ApiSettings()).openapi()["paths"]


def test_tokens_are_compared_in_constant_time(monkeypatch):
    calls = []
    real = secrets.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr("grocery_planner.api.app.secrets.compare_digest", spy)
    app, *_ = counting()
    with TestClient(app) as client:
        client.get("/internal/owner", params={"token": "wrong"})
        client.get("/internal/owner", params={"token": OWNER_TOKEN})
        post(client)
    assert (b"wrong", OWNER_TOKEN.encode()) in calls
    assert len(calls) >= 3  # both tokens, and the cookie check on the run


def test_a_wrong_owner_cookie_is_not_the_owner():
    app, _, store, visitors = counting()
    with TestClient(app) as client:
        client.cookies.set(OWNER_COOKIE, "made-up")
        post(client)
        finish(store)
    assert [r["is_owner"] for r in visitors.rows.values()] == [False]
    assert [r.is_owner for r in store.runs.values()] == [False]


def test_the_owner_is_marked_and_left_out_of_the_stats():
    app, _, store, visitors = counting()
    with TestClient(app) as owner, TestClient(app) as guest, TestClient(app) as other:
        assert owner.get("/internal/owner", params={"token": OWNER_TOKEN}).status_code == 204
        post(owner), post(owner)
        post(guest), post(guest)
        post(other)
        finish(store)
        mine = visitors.rows[visitor_hash(SECRET, cookie_of(owner))]
        assert mine["is_owner"] is True and mine["run_count"] == 2
        stats = owner.get("/internal/stats", headers=AUTH).json()
    assert sum(r.is_owner for r in store.runs.values()) == 2
    assert stats["total_runs"] == 3  # guest 2, other 1; the owner's two are left out
    assert stats["runs_by_status"] == {"completed": 3, "stopped": 0, "failed": 0}
    assert stats["unique_visitors"] == 2 and stats["returning_visitors"] == 1
    assert sum(d["runs"] for d in stats["runs_per_day"]) <= 3


def test_the_owner_mark_sticks_once_set():
    visitors = InMemoryVisitors()
    visitors.record_run("h", owner=True)
    visitors.record_run("h", owner=False)
    assert visitors.rows["h"]["is_owner"] is True and visitors.rows["h"]["run_count"] == 2


# ---- the stats -----------------------------------------------------------------------------------


def test_stats_numbers():
    app, _, store, visitors = counting()
    with TestClient(app) as a, TestClient(app) as b:
        post(a), post(a), post(b)
        finish(store)
        stats = a.get("/internal/stats", headers=AUTH).json()
    assert set(stats) == {
        "total_runs", "runs_by_status", "runs_per_day", "unique_visitors", "returning_visitors",
    }  # fmt: skip
    assert stats["total_runs"] == 3 and stats["runs_by_status"]["completed"] == 3
    assert stats["unique_visitors"] == 2 and stats["returning_visitors"] == 1
    days = stats["runs_per_day"]
    assert len(days) == 30 and [d["day"] for d in days] == sorted(d["day"] for d in days)
    today = visitors.stats(datetime.now(SYDNEY).date())  # the app's own date is patched in tests
    assert today["runs_per_day"][-1]["runs"] == 3  # today, in Sydney


def test_stats_count_each_final_status():
    app, _, store, visitors = counting()
    with TestClient(app) as client:
        post(client), post(client), post(client)
        finish(store)
        ids = list(store.runs)
        store.update_run(ids[1], status="stopped")
        store.update_run(ids[2], status="failed")
        stats = client.get("/internal/stats", headers=AUTH).json()
    assert stats["runs_by_status"] == {"completed": 1, "stopped": 1, "failed": 1}
    assert stats["total_runs"] == 3


@pytest.mark.parametrize(
    ("headers", "status"),
    [({}, 401), ({"Authorization": "Basic abc"}, 401), ({"Authorization": "Bearer nope"}, 403)],
)
def test_stats_need_the_schedulers_identity(headers, status):
    app, *_ = counting()
    with TestClient(app) as client:
        r = client.get("/internal/stats", headers=headers)
    assert r.status_code == status and "total_runs" not in r.text


def test_stats_use_the_same_check_as_the_daily_job():
    app, services, *_ = counting()
    services.verify_token, seen = verifier()
    with TestClient(app) as client:
        assert client.get("/internal/stats", headers=AUTH).status_code == 200
    assert seen == [("good", AUDIENCE, SCHEDULER)]


def test_stats_fail_closed_when_not_configured():
    for kw in ({"daily_audience": None}, {"daily_sa_email": None}):
        app, *_ = counting(**kw)
        with TestClient(app) as client:
            assert client.get("/internal/stats", headers=AUTH).status_code == 503
    app, services, *_ = counting()
    services.visitors = None
    with TestClient(app) as client:
        assert client.get("/internal/stats", headers=AUTH).status_code == 503
    assert "/internal/stats" not in create_app(ApiSettings()).openapi()["paths"]


# ---- privacy -------------------------------------------------------------------------------------


def test_no_hash_and_no_raw_id_in_any_response_log_or_run_record(caplog):
    app, _, store, visitors = counting()
    seen = []
    with TestClient(app) as client, caplog.at_level(logging.DEBUG):
        client.get("/internal/owner", params={"token": OWNER_TOKEN})
        created = post(client)
        run_id = created.json()["run_id"]
        finish(store)
        raw = cookie_of(client)
        for r in (
            created,
            client.get(f"/api/runs/{run_id}"),
            client.get(f"/api/runs/{run_id}/steps"),
            client.get(f"/api/runs/{run_id}/events"),
            client.get("/api/availability"),
            client.get("/internal/stats", headers=AUTH),
            client.get("/health"),
        ):
            seen.append(r.text + str(dict(r.headers)))
    (stored,) = visitors.rows
    assert stored == visitor_hash(SECRET, raw) and raw not in stored
    everything = "\n".join(seen)
    assert stored not in everything and stored not in caplog.text
    assert raw not in "".join(s for s in seen[1:]) and raw not in caplog.text
    # the cookie itself is the browser's own copy, set by the first answer only
    assert raw in seen[0] and stored not in seen[0]
    records = json.dumps([r.model_dump(mode="json") for r in store.runs.values()], default=str)
    assert stored not in records and raw not in records  # no run record holds a visitor id
    assert raw not in json.dumps(visitors.rows, default=str)  # never stored raw
