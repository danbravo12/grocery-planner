"""The FastAPI application. `create_app(settings, services)` is the whole API; tests build it
with fake models and an in-memory store, the real one is built from the environment."""

import asyncio
import json
import logging
import re
import secrets
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Protocol

from fastapi import FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import Field

from grocery_planner.api import schemas
from grocery_planner.api.daily import TokenRejected, run_daily, verify_google_oidc
from grocery_planner.api.errors import ApiError, install_error_handlers
from grocery_planner.api.queue import RunQueue
from grocery_planner.api.settings import ApiSettings
from grocery_planner.api.views import run_view, step_view
from grocery_planner.domain import PlannerConfig
from grocery_planner.domain.request import PlanRequest
from grocery_planner.guardrails.budget import AdmitResult
from grocery_planner.guardrails.clock import Availability
from grocery_planner.orchestrator import ESTIMATED_REQUESTS, BudgetUnavailable, Runner
from grocery_planner.orchestrator.events import events_from_state
from grocery_planner.orchestrator.models import TERMINAL
from grocery_planner.tools.price_lookup import today_sydney

log = logging.getLogger(__name__)
VISITOR_COOKIE = "gp_visitor"
_VISITOR_ID = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


class DayBudget(Protocol):
    """The daily budget as the API uses it: the BudgetGuard plus availability and admit."""

    def reserve(self, run_id: str, estimated_requests: int) -> None: ...
    def release(self, run_id: str) -> None: ...
    def availability(self, now: datetime | None = None) -> Availability: ...
    def admit(self, visitor_id: str, ip: str, now: datetime | None = None) -> AdmitResult: ...
    def prune(self, keep_days: int = 7, now: datetime | None = None) -> int: ...


@dataclass
class Services:
    runner: Runner
    budget: DayBudget
    queue: RunQueue | None = None  # built from the settings when not given
    seed: Callable[[date], int] | None = None  # re-seed the price catalogue (None: no database)
    verify_token: Callable[[str, str, str], None] = verify_google_oidc  # (token, audience, email)


class CreateRun(PlanRequest):
    """The POST body: a plan request plus optional advanced settings."""

    parameters: PlannerConfig = Field(default_factory=PlannerConfig)


# ---- request helpers ---------------------------------------------------------------------------


class BodyLimitMiddleware:
    """Refuse a request body over `limit` bytes with a 413, whether or not it declares its length.
    A pure ASGI middleware, so the limit applies before any body parsing. (FastAPI turns an
    exception raised while it reads the body into a 400, so a body that grows past the limit is
    cut short instead and whatever the app answers is replaced by the 413.)"""

    def __init__(self, app: Any, limit: int) -> None:
        self.app, self.limit = app, limit
        self._body = json.dumps(
            {"code": "body_too_large", "message": f"The request body is limited to {limit} bytes."}
        ).encode()

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = dict(scope["headers"]).get(b"content-length", b"")
        if declared.isdigit() and int(declared) > self.limit:
            await self._reject(send)
            return
        seen, too_large = 0, False

        async def limited_receive() -> Any:
            nonlocal seen, too_large
            if too_large:
                return {"type": "http.request", "body": b"", "more_body": False}
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.limit:
                    too_large = True
                    return {"type": "http.request", "body": b"", "more_body": False}
            return message

        async def replacing_send(message: Any) -> None:
            if too_large:  # drop the app's own answer; send the 413 once
                if message["type"] == "http.response.start":
                    await self._reject(send)
                return
            await send(message)

        await self.app(scope, limited_receive, replacing_send)

    async def _reject(self, send: Any) -> None:
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(self._body)).encode()),
        ]
        await send({"type": "http.response.start", "status": 413, "headers": headers})
        await send({"type": "http.response.body", "body": self._body})


def client_ip(request: Request, trusted_proxy: bool) -> str:
    """The socket address, or behind a trusted proxy the last X-Forwarded-For entry: that is the
    one our own proxy appended; earlier entries are whatever the client claimed."""
    if trusted_proxy:
        forwarded = request.headers.get("x-forwarded-for", "")
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if parts:
            return parts[-1]
    return request.client.host if request.client else "unknown"


def visitor_id_of(request: Request) -> tuple[str, bool]:
    """(id, is_new). A missing or malformed cookie gets a fresh random id."""
    existing = request.cookies.get(VISITOR_COOKIE, "")
    if _VISITOR_ID.match(existing):
        return existing, False
    return secrets.token_urlsafe(24), True


def sse(event_type: str, data: Any, event_id: int | None = None) -> str:
    head = f"event: {event_type}\n" + (f"id: {event_id}\n" if event_id is not None else "")
    return head + f"data: {json.dumps(data, default=str)}\n\n"


def _uuid_or_404(run_id: str) -> str:
    try:
        return str(uuid.UUID(run_id))
    except ValueError:
        raise ApiError(404, "not_found", "No such run.") from None


# ---- the app -----------------------------------------------------------------------------------


def create_app(settings: ApiSettings | None = None, services: Services | None = None) -> FastAPI:
    settings = settings or ApiSettings()
    settings.validate()
    if services is not None and services.queue is None:
        services.queue = RunQueue(
            lambda run_id: _work(services, run_id), settings.max_workers, settings.max_queued
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        tasks: list[asyncio.Task] = []
        if services is not None:
            services.queue.start()
            tasks = [
                asyncio.create_task(
                    _every(settings.sweep_interval_seconds, lambda: _sweep(services))
                ),
                asyncio.create_task(
                    _every(
                        settings.prune_interval_seconds,
                        lambda: services.budget.prune(settings.prune_keep_days),
                    )
                ),
            ]
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            for t in tasks:
                with suppress(asyncio.CancelledError):
                    await t
            if services is not None:
                services.queue.stop()

    app = FastAPI(
        title="Grocery Planner",
        lifespan=lifespan,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )
    install_error_handlers(app)
    app.add_middleware(BodyLimitMiddleware, limit=settings.max_body_bytes)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.frontend_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "Last-Event-ID"],
    )

    def need() -> Services:
        if services is None:
            raise ApiError(503, "not_configured", "The planner is not configured.")
        return services

    @app.get("/health", response_model=schemas.Health)
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/internal/daily", include_in_schema=False)
    async def daily(request: Request) -> dict[str, Any]:
        """Cloud Scheduler's daily job. Fails closed: refused unless it is configured and the
        caller proves to be the scheduler's service account."""
        s = need()
        if not (settings.daily_audience and settings.daily_sa_email):
            raise ApiError(503, "not_configured", "The daily job is not configured.")
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise ApiError(401, "unauthorized", "A Google identity token is required.")
        try:
            await run_in_threadpool(
                s.verify_token, token.strip(), settings.daily_audience, settings.daily_sa_email
            )
        except TokenRejected as exc:
            log.warning("daily job refused: %s", exc)
            raise ApiError(403, "forbidden", "Not allowed.") from None
        result = await run_in_threadpool(run_daily, s, today_sydney(), settings.prune_keep_days)
        logging.getLogger("uvicorn.error").info("daily job: %s", result)  # uvicorn logs INFO
        return result

    @app.get("/api/availability", response_model=schemas.Availability)
    async def availability() -> dict[str, Any]:
        s = need()
        a = await run_in_threadpool(s.budget.availability)
        return {
            "available": a.available,
            "reason": a.reason,
            "message": a.message,
            "reset_at_utc": a.reset_at_utc.isoformat(),
            "reset_at_sydney": a.reset_at_sydney.isoformat(),
            "sample_run_id": settings.sample_run_id,
        }

    @app.post(
        "/api/runs",
        status_code=202,
        response_model=schemas.RunAccepted,
        responses={
            **{c: {"model": schemas.ErrorBody} for c in (400, 413, 429, 503)},
            422: {"model": schemas.ValidationErrorBody},
        },
    )
    async def create_run(body: CreateRun, request: Request, response: Response) -> dict[str, str]:
        s = need()
        # 1. validate: FastAPI has parsed the body (422 with field messages if it is not valid)
        # 2. is the service open today?
        a = await run_in_threadpool(s.budget.availability)
        if not a.available:
            wait = max(1, int((a.reset_at_utc - datetime.now(UTC)).total_seconds()))
            raise ApiError(
                503, "unavailable", a.message, headers={"Retry-After": str(wait)},
                reason=a.reason, reset_at_utc=a.reset_at_utc.isoformat(),
                reset_at_sydney=a.reset_at_sydney.isoformat(),
            )  # fmt: skip
        if not s.queue.has_room():
            raise _busy()
        visitor, new_cookie = visitor_id_of(request)
        ip = client_ip(request, settings.trusted_proxy)
        # 3. reserve, 4. admit, 5. enqueue (see _accept)
        run_id = await run_in_threadpool(_accept, s, body, visitor, ip)
        if new_cookie:
            response.set_cookie(
                VISITOR_COOKIE, visitor, max_age=365 * 24 * 3600, httponly=True,
                secure=settings.is_prod, samesite="lax", path="/",
            )  # fmt: skip
        origin = settings.frontend_origins[0] if settings.frontend_origins else ""
        return {
            "run_id": run_id,
            "run_url": f"{origin}/runs/{run_id}",
            "events_url": f"/api/runs/{run_id}/events",
        }

    @app.get(
        "/api/runs/{run_id}",
        response_model=schemas.RunView,
        responses={404: {"model": schemas.ErrorBody}},
    )
    async def get_run(run_id: str) -> dict[str, Any]:
        s = need()
        run, _ = await _load(s, run_id)
        return run_view(run)

    @app.get(
        "/api/runs/{run_id}/steps",
        response_model=schemas.StepsView,
        responses={404: {"model": schemas.ErrorBody}},
    )
    async def get_steps(run_id: str) -> dict[str, Any]:
        s = need()
        _, steps = await _load(s, run_id)
        return {"run_id": run_id, "steps": [step_view(x) for x in steps]}

    @app.get(
        "/api/runs/{run_id}/events",
        response_class=StreamingResponse,
        responses={200: {"content": {"text/event-stream": {}}}, 404: {"model": schemas.ErrorBody}},
    )
    async def events(run_id: str, request: Request, after: int | None = None) -> StreamingResponse:
        s = need()
        run_id = _uuid_or_404(run_id)
        await _load(s, run_id)  # 404 before the stream starts
        header = request.headers.get("last-event-id", "")
        last = after if after is not None else (int(header) if header.isdigit() else 0)

        async def stream() -> AsyncIterator[str]:
            seq, quiet_since, queued_at = last, time.monotonic(), None
            yield ": connected\n\n"
            while True:
                run, steps = await _load(s, run_id)
                for e in events_from_state(run, steps, seq):
                    seq, quiet_since = e.seq, time.monotonic()
                    yield sse(e.type, e.model_dump(mode="json"), e.seq)
                if run.status in TERMINAL:
                    return
                if run.status == "queued":
                    pos = s.queue.position(run_id)
                    if pos is not None and pos != queued_at:
                        queued_at, quiet_since = pos, time.monotonic()
                        yield sse("queued", {"run_id": run_id, "position": pos})
                if await request.is_disconnected():
                    return
                if time.monotonic() - quiet_since >= settings.heartbeat_seconds:
                    quiet_since = time.monotonic()
                    yield ": heartbeat\n\n"
                await asyncio.sleep(settings.poll_seconds)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    def _busy() -> ApiError:
        return ApiError(
            503, "busy", "Too many plans are running right now. Please try again in a minute.",
            headers={"Retry-After": "30"},
        )  # fmt: skip

    def _accept(s: Services, body: CreateRun, visitor: str, ip: str) -> str:
        """Reserve the budget, admit the visitor, queue the run. The run row is written first
        because the reservation is recorded against it; a refusal removes it again."""
        request = PlanRequest.model_validate(body.model_dump(exclude={"parameters"}))
        run_id = s.runner.create_run(request, body.parameters, today_sydney())

        def refuse(error: ApiError, release: bool) -> ApiError:
            if release:
                s.budget.release(run_id)
            s.runner.store.delete_run(run_id)
            return error

        try:
            s.budget.reserve(run_id, ESTIMATED_REQUESTS)
        except BudgetUnavailable as exc:  # busy | day_budget: refused before admit
            if exc.reason == "busy":
                raise refuse(_busy(), release=False) from None
            raise refuse(
                ApiError(503, "day_budget", "The planner has used up today's AI capacity."),
                release=False,
            ) from None
        result = s.budget.admit(visitor, ip)
        if not result.ok:
            message = (
                "You have reached today's limit of plans. Please try again tomorrow."
                if result.reason == "visitor_limit"
                else "Too many plans have been made from your network today."
            )
            raise refuse(ApiError(429, result.reason or "limit", message), release=True)
        if not s.queue.try_enqueue(run_id):
            raise refuse(_busy(), release=True)
        return run_id

    async def _load(s: Services, run_id: str):
        run_id = _uuid_or_404(run_id)
        try:
            return await run_in_threadpool(
                lambda: (s.runner.store.get_run(run_id), s.runner.store.get_steps(run_id))
            )
        except KeyError:
            raise ApiError(404, "not_found", "No such run.") from None

    dist = settings.frontend_dist
    if dist is not None and (dist / "index.html").is_file():
        root = dist.resolve()

        @app.get("/{path:path}", include_in_schema=False)
        async def frontend(path: str) -> FileResponse:
            """The single-page app: a file from the build if there is one, otherwise index.html
            so client-side routes (/runs/<id>) survive a reload. Never for /api."""
            if path == "api" or path.startswith("api/"):
                raise ApiError(404, "not_found", "Not found.")
            target = (root / path).resolve()
            if path and target.is_file() and root in target.parents:
                immutable = path.startswith("assets/")  # hashed file names
                cache = "public, max-age=31536000, immutable" if immutable else "no-cache"
                return FileResponse(target, headers={"Cache-Control": cache})
            return FileResponse(root / "index.html", headers={"Cache-Control": "no-cache"})

    return app


def _sweep(s: Services) -> None:
    """Close runs that will never finish: running ones that stopped writing, and queued ones that
    this process does not hold (it restarted) or that waited too long."""
    s.runner.sweep_stale()
    s.runner.sweep_queued(lambda run_id: s.queue.position(run_id) is not None)


def _work(s: Services, run_id: str) -> None:
    """One worker's job: run the plan (the budget was reserved when the request was accepted)."""
    try:
        if s.runner.store.get_run(run_id).status != "queued":
            s.budget.release(run_id)  # swept while it waited: do not start it
            return
        s.runner.run_plan(run_id, already_reserved=True)
    except Exception:
        log.exception("run %s crashed", run_id)
        with suppress(Exception):
            s.budget.release(run_id)


async def _every(seconds: float, job) -> None:
    """Run `job` now and then every `seconds`, in a thread, forever. A failure is logged only."""
    while True:
        try:
            await run_in_threadpool(job)
        except Exception:
            log.exception("maintenance job failed")
        await asyncio.sleep(seconds)
