"""The FastAPI application. `create_app(settings, services)` is the whole API; tests build it
with fake models and an in-memory store, the real one is built from the environment."""

import asyncio
import json
import logging
import re
import secrets
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from fastapi import FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import Field, ValidationError

from grocery_planner.api.errors import ApiError, install_error_handlers, validation_fields
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


class CreateRun(PlanRequest):
    """The POST body: a plan request plus optional advanced settings."""

    parameters: PlannerConfig = Field(default_factory=PlannerConfig)


# ---- request helpers ---------------------------------------------------------------------------


async def read_json_body(request: Request, limit: int) -> Any:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise ApiError(413, "body_too_large", f"The request body is limited to {limit} bytes.")
    chunks, total = [], 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise ApiError(413, "body_too_large", f"The request body is limited to {limit} bytes.")
        chunks.append(chunk)
    try:
        return json.loads(b"".join(chunks) or b"null")
    except (ValueError, UnicodeDecodeError):
        raise ApiError(400, "invalid_json", "The request body must be valid JSON.") from None


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
                    _every(settings.sweep_interval_seconds, services.runner.sweep_stale)
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

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/availability")
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

    @app.post("/api/runs", status_code=202)
    async def create_run(request: Request, response: Response) -> dict[str, str]:
        s = need()
        # 1. validate
        payload = await read_json_body(request, settings.max_body_bytes)
        try:
            body = CreateRun.model_validate(payload)
        except ValidationError as exc:
            raise ApiError(
                422, "validation_error", "The request is not valid.",
                fields=validation_fields(exc.errors()),
            ) from None  # fmt: skip
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

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        s = need()
        run, _ = await _load(s, run_id)
        return run_view(run)

    @app.get("/api/runs/{run_id}/steps")
    async def get_steps(run_id: str) -> dict[str, Any]:
        s = need()
        _, steps = await _load(s, run_id)
        return {"run_id": run_id, "steps": [step_view(x) for x in steps]}

    @app.get("/api/runs/{run_id}/events")
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

    return app


def _work(s: Services, run_id: str) -> None:
    """One worker's job: run the plan (the budget was reserved when the request was accepted)."""
    try:
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
