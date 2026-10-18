"""POST /internal/daily: only the scheduler's service account may call it, and it does the
daily work (seed with today's Sydney date, sweep, prune)."""

from datetime import date

import pytest
from fastapi.testclient import TestClient

from grocery_planner.api import ApiSettings, create_app
from grocery_planner.api.daily import TokenRejected, verify_google_oidc
from tests.test_api import FAST, build

AUDIENCE = "https://grocery-planner-123.australia-southeast1.run.app"
SCHEDULER = "scheduler@proj.iam.gserviceaccount.com"
AS_OF = date(2026, 10, 18)


def settings(**kw):
    base = dict(daily_audience=AUDIENCE, daily_sa_email=SCHEDULER, **FAST)
    return ApiSettings(**{**base, **kw})


def verifier(good_token="good"):
    seen = []

    def verify(token, audience, email):
        seen.append((token, audience, email))
        if token != good_token:
            raise TokenRejected("bad token")

    return verify, seen


def client_with(seeded=None, **kw):
    app, services, store, budget = build(settings=settings(**kw))
    services.verify_token, seen = verifier()
    services.seed = lambda anchor: (seeded.append(anchor) or 42) if seeded is not None else 42
    return TestClient(app), services, seen


def test_the_scheduler_gets_the_job_done_and_the_token_is_checked_against_our_settings():
    seeded: list[date] = []
    client, _, seen = client_with(seeded)
    with client:
        r = client.post("/internal/daily", headers={"Authorization": "Bearer good"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["seeded_products"] == 42
    assert body["swept_stale"] == 0 and body["swept_queued"] == 0
    assert body["pruned_counters"] == 0 and body["anchor"] == AS_OF.isoformat()
    assert seeded == [AS_OF]  # today in Sydney
    assert seen == [("good", AUDIENCE, SCHEDULER)]


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({}, 401),
        ({"Authorization": "Basic abc"}, 401),
        ({"Authorization": "Bearer "}, 401),
        ({"Authorization": "Bearer nope"}, 403),
    ],
)
def test_everything_else_is_refused_and_nothing_runs(headers, status):
    seeded: list[date] = []
    client, _, _ = client_with(seeded)
    with client:
        r = client.post("/internal/daily", headers=headers)
    assert r.status_code == status and seeded == []
    assert "seeded_products" not in r.text


def test_it_fails_closed_when_the_audience_or_account_is_not_configured():
    for kw in ({"daily_audience": None}, {"daily_sa_email": None}):
        app, services, *_ = build(settings=settings(**kw))
        services.verify_token, _ = verifier()
        with TestClient(app) as c:
            r = c.post("/internal/daily", headers={"Authorization": "Bearer good"})
        assert r.status_code == 503


def test_get_is_not_the_job_and_the_route_is_not_in_the_api_schema():
    client, *_ = client_with()
    with client:
        assert client.get("/internal/daily").status_code in (404, 405)
    assert "/internal/daily" not in create_app(ApiSettings(app_env="dev")).openapi()["paths"]


def test_one_part_failing_is_reported_and_does_not_skip_the_rest():
    app, services, *_ = build(settings=settings())
    services.verify_token, _ = verifier()

    def boom(anchor):
        raise RuntimeError("database down")

    services.seed = boom
    with TestClient(app) as c:
        body = c.post("/internal/daily", headers={"Authorization": "Bearer good"}).json()
    assert body["ok"] is False and body["seeded_products"] == "failed"
    assert body["pruned_counters"] == 0


def test_the_real_verifier_rejects_garbage_without_a_network_call(monkeypatch):
    monkeypatch.setattr(
        "google.oauth2.id_token.verify_oauth2_token",
        lambda token, request, audience=None: (_ for _ in ()).throw(ValueError("bad")),
    )
    with pytest.raises(TokenRejected):
        verify_google_oidc("x.y.z", AUDIENCE, SCHEDULER)


def _claims(monkeypatch, **claims):
    base = {
        "iss": "https://accounts.google.com",
        "aud": AUDIENCE,
        "email": SCHEDULER,
        "email_verified": True,
    }
    monkeypatch.setattr(
        "google.oauth2.id_token.verify_oauth2_token",
        lambda token, request, audience=None: {**base, **claims},
    )


def test_the_real_verifier_checks_issuer_audience_and_email(monkeypatch):
    _claims(monkeypatch)
    verify_google_oidc("t", AUDIENCE, SCHEDULER)  # all three right
    _claims(monkeypatch, email=SCHEDULER.upper())
    verify_google_oidc("t", AUDIENCE, SCHEDULER)  # email case does not matter
    for bad in (
        {"iss": "https://evil.example"},
        {"aud": "https://other.run.app"},
        {"email": "someone@else.com"},
        {"email_verified": False},
    ):
        _claims(monkeypatch, **bad)
        with pytest.raises(TokenRejected):
            verify_google_oidc("t", AUDIENCE, SCHEDULER)
