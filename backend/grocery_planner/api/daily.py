"""The daily job (POST /internal/daily), called by Cloud Scheduler with a Google OIDC token.

Only the scheduler's service account may call it: the token must be signed by Google, issued by
accounts.google.com, addressed to this service's URL (the audience) and belong to the one
configured service account email. Anything else is refused. The job re-seeds the prices with
today's Sydney date (their specials are relative dates), sweeps runs that will never finish,
prunes old counters, and reads the database (the writes keep a free Supabase project awake)."""

import logging
from datetime import date
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from grocery_planner.api.app import Services

log = logging.getLogger(__name__)
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")


class TokenRejected(Exception):
    """The caller is not the scheduler (the reason is for the log, never for the caller)."""


def verify_google_oidc(token: str, audience: str, email: str) -> None:
    """Raise TokenRejected unless `token` is a valid Google ID token for `audience` from the
    service account `email`."""
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    try:
        claims = id_token.verify_oauth2_token(token, google_requests.Request(), audience=audience)
    except ValueError as exc:  # bad signature, expired, wrong audience or issuer, malformed
        raise TokenRejected(f"token not valid: {exc}") from exc
    if claims.get("iss") not in GOOGLE_ISSUERS:
        raise TokenRejected("wrong issuer")
    if claims.get("aud") != audience:
        raise TokenRejected("wrong audience")
    if not claims.get("email_verified") or str(claims.get("email", "")).lower() != email.lower():
        raise TokenRejected("not the scheduler's service account")


def run_daily(s: "Services", anchor: date, keep_days: int) -> dict[str, Any]:
    """Seed, sweep, prune. Each part is reported; one failing does not skip the others."""
    result: dict[str, Any] = {"anchor": anchor.isoformat()}

    def step(name: str, job) -> None:
        try:
            result[name] = job()
        except Exception:
            log.exception("daily job: %s failed", name)
            result[name] = "failed"

    step("seeded_products", lambda: s.seed(anchor) if s.seed else "skipped (no database)")
    step("swept_stale", lambda: len(s.runner.sweep_stale()))
    step(
        "swept_queued",
        lambda: len(s.runner.sweep_queued(lambda run_id: s.queue.position(run_id) is not None)),
    )
    step("pruned_counters", lambda: s.budget.prune(keep_days))
    result["ok"] = "failed" not in result.values()
    return result
