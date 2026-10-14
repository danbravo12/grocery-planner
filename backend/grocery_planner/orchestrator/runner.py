"""The orchestrator: ingredient agent -> price agent -> stop check -> planner, with at most one
loop-back (reprice, then the planner once more). Every step is written to run_steps as it
happens, so a run does not depend on any client being connected.
"""

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from pydantic import BaseModel
from pydantic_core import to_jsonable_python

from grocery_planner.agents.ingredient import INGREDIENT_PROMPT_VERSION, run_ingredient_agent
from grocery_planner.agents.planner import PLANNER_PROMPT_VERSION, run_planner
from grocery_planner.agents.price import (
    PRICE_PROMPT_VERSION,
    PriceAgentResult,
    price_items,
    reprice,
)
from grocery_planner.config import ACTIVE_STORES
from grocery_planner.db.dish_cache import DishCacheRepo
from grocery_planner.domain import PlannerConfig, Store
from grocery_planner.domain.ingredients import IngredientList
from grocery_planner.domain.request import PlanRequest
from grocery_planner.llm.types import (
    BudgetExhausted,
    LLMClient,
    LLMError,
    LLMUnavailable,
    ModelHooks,
    QuotaExhausted,
    SchemaValidationFailed,
    Usage,
)
from grocery_planner.orchestrator.budget import (
    ESTIMATED_REQUESTS,
    BudgetGuard,
    BudgetUnavailable,
    NoopBudgetGuard,
)
from grocery_planner.orchestrator.events import events_from_state
from grocery_planner.orchestrator.models import RunEvent, RunRecord, StepRecord
from grocery_planner.orchestrator.store import RunStore
from grocery_planner.tools.planner_math import coverage, should_stop
from grocery_planner.tools.price_lookup import PriceSource

log = logging.getLogger(__name__)
LLMFactory = Callable[[ModelHooks], LLMClient]


@dataclass(frozen=True)
class RunnerSettings:
    deadline_seconds: float = 180.0  # whole-run limit, checked before and after every step
    stale_after: timedelta = timedelta(minutes=5)  # a running run with no heartbeat this long


class RunTimeout(Exception):
    pass


class Deadline:
    def __init__(self, seconds: float, clock: Callable[[], float]) -> None:
        self._seconds, self._clock, self._start = seconds, clock, clock()

    def check(self) -> None:
        if self._clock() - self._start > self._seconds:
            raise RunTimeout(f"run exceeded {self._seconds:g}s")

    def elapsed_ms(self) -> int:
        return round((self._clock() - self._start) * 1000)


def _json(obj: Any) -> Any:
    return obj.model_dump(mode="json") if isinstance(obj, BaseModel) else obj


def merge_price_results(first: PriceAgentResult, second: PriceAgentResult) -> PriceAgentResult:
    """Replace the re-priced items' rows, flags, downgrades and reasons; keep everything else."""
    redone = {r.item_id for r in second.items}
    new_rows = {(r.item_id, r.store): r for r in second.items}
    return first.model_copy(
        update=dict(
            items=[new_rows.get((r.item_id, r.store), r) for r in first.items],
            downgrades=[d for d in first.downgrades if d.item_id not in redone] + second.downgrades,
            flags=[f for f in first.flags if f.item_id not in redone] + second.flags,
            reasons={**first.reasons, **second.reasons},
            usage=first.usage + second.usage,
            rounds=first.rounds + second.rounds,
            model=second.model,
            fell_back=first.fell_back or second.fell_back,
        )
    )


# ---- user-facing wording for failures ---------------------------------------------------------

_LIST_NOTE = " Your ingredient list is below, so you can still shop from it."


def classify_failure(exc: Exception) -> tuple[str, str]:
    """(failure_reason, user message without the list note). Never includes exception text."""
    if isinstance(exc, BudgetUnavailable):
        if exc.reason == "busy":
            return (
                "busy",
                "Too many plans are running right now. Please try again in a few minutes.",
            )
        return (
            "day_budget",
            "The planner has used up today's AI capacity. Please try again after the daily reset.",
        )
    if isinstance(exc, BudgetExhausted):
        return (
            "budget_exhausted",
            "Today's AI capacity ran out while this plan was running. "
            "Please try again after the daily reset.",
        )
    if isinstance(exc, QuotaExhausted):
        if exc.kind == "per_day":
            return (
                "daily_quota",
                "The service has used up today's AI quota. Please try again after the daily reset.",
            )
        return "rate_limited", "The AI service is busy right now. Please try again in a minute."
    if isinstance(exc, LLMUnavailable):
        return (
            "model_unavailable",
            "The AI service is unavailable right now. Please try again later.",
        )
    if isinstance(exc, SchemaValidationFailed):
        return (
            "schema_validation",
            "One of the steps could not produce a valid answer. Please try again.",
        )
    if isinstance(exc, LLMError):
        return "model_error", "The AI service returned an error. Please try again later."
    if isinstance(exc, RunTimeout):
        return "timeout", "This took too long and was stopped. Please try again."
    return "internal", "Something went wrong on our side. Please try again later."


def _public_error(exc: Exception) -> str:
    """What goes into run_steps.error (shown on public traces): a class name, never a trace."""
    if isinstance(exc, BudgetExhausted):
        return "BudgetExhausted"
    if isinstance(exc, QuotaExhausted):
        return f"QuotaExhausted({exc.kind})"
    if isinstance(exc, BudgetUnavailable):
        return f"BudgetUnavailable({exc.reason})"
    if isinstance(exc, (LLMError, RunTimeout)):
        return type(exc).__name__
    return f"internal error ({type(exc).__name__})"


class Runner:
    def __init__(
        self,
        store: RunStore,
        llm_factory: LLMFactory,
        cache: DishCacheRepo,
        source: PriceSource,
        budget: BudgetGuard | None = None,
        settings: RunnerSettings | None = None,
        stores: tuple[Store, ...] = ACTIVE_STORES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store, self.cache, self.source = store, cache, source
        self.budget = budget or NoopBudgetGuard()
        self.settings = settings or RunnerSettings()
        self.stores, self._clock, self._llm_factory = stores, clock, llm_factory

    # ---- creating and starting runs -----------------------------------------------------

    def create_run(
        self, request: PlanRequest, config: PlannerConfig, as_of: date, bypass_cache: bool = False
    ) -> str:
        """Write the queued run row first: a long random id, the request and the parameters used."""
        parameters = {
            "config": config.model_dump(mode="json"),
            "as_of": as_of.isoformat(),
            "bypass_cache": bypass_cache,
            "deadline_seconds": self.settings.deadline_seconds,
            "stores": [s.value for s in self.stores],
            "prompt_versions": {
                "ingredient": INGREDIENT_PROMPT_VERSION,
                "price": PRICE_PROMPT_VERSION,
                "planner": PLANNER_PROMPT_VERSION,
            },
        }
        return self.store.create_run(
            request.model_dump(mode="json"), parameters, ESTIMATED_REQUESTS
        )

    def submit(self, run_id: str) -> threading.Thread:
        """Run in a background thread; the caller (an API request, say) can go away."""
        thread = threading.Thread(target=self._run_safely, args=(run_id,), name=f"run-{run_id}")
        thread.start()
        return thread

    def start(
        self, request: PlanRequest, config: PlannerConfig, as_of: date, bypass_cache: bool = False
    ) -> str:
        run_id = self.create_run(request, config, as_of, bypass_cache)
        self.submit(run_id)
        return run_id

    def _run_safely(self, run_id: str) -> None:
        try:
            self.run_plan(run_id)
        except Exception:  # run_plan records its own failures; this is the last resort
            log.exception("run %s crashed outside the pipeline", run_id)

    def events(self, run_id: str, after_seq: int = 0) -> list[RunEvent]:
        return events_from_state(
            self.store.get_run(run_id), self.store.get_steps(run_id), after_seq
        )

    def sweep_stale(self, now: datetime | None = None) -> list[str]:
        """Fail runs that say 'running' but have not written anything for `stale_after`."""
        now = now or datetime.now(UTC)
        swept = []
        for run_id in self.store.running_not_updated_since(now - self.settings.stale_after):
            if self.store.get_run(run_id).status != "running":
                continue
            self.store.update_run(
                run_id, status="failed", failure_reason="stale", completed_at=now,
                user_message="This run stopped responding and was closed. Please try again.",
            )  # fmt: skip
            self.budget.release(run_id)  # give back whatever the dead run had reserved
            swept.append(run_id)
        return swept

    # ---- the run ------------------------------------------------------------------------

    def run_plan(self, run_id: str) -> RunRecord:
        """Run the whole plan synchronously and return the final run row."""
        run = self.store.get_run(run_id)
        request = PlanRequest.model_validate(run.input)
        params = run.parameters
        config = PlannerConfig.model_validate(params["config"])
        as_of = date.fromisoformat(params["as_of"])
        deadline = Deadline(
            params.get("deadline_seconds", self.settings.deadline_seconds), self._clock
        )
        self.store.update_run(run_id, status="running")
        try:
            self.budget.reserve(run_id, run.estimated_requests)
        except BudgetUnavailable as exc:  # not admitted: the run never starts
            self._fail(run_id, exc, deadline)
            return self.store.get_run(run_id)
        try:
            llm = self._llm_factory(
                ModelHooks(
                    on_request=lambda usage: self.budget.record(
                        run_id, usage.model, usage.requests
                    ),
                    before_request=self.budget.before_request,
                    fallback_allowed=self.budget.fallback_allowed,
                    on_quota_exhausted=self.budget.quota_exhausted,
                )
            )
            self._pipeline(
                run_id, request, config, as_of, params.get("bypass_cache", False), llm, deadline
            )
        except Exception as exc:
            if not isinstance(exc, (LLMError, RunTimeout)):
                log.exception("run %s failed with an unexpected error", run_id)
            self._fail(run_id, exc, deadline)
        finally:
            self.budget.release(run_id)
        return self.store.get_run(run_id)

    def _pipeline(self, run_id, request, config, as_of, bypass_cache, llm, deadline) -> None:
        ing: IngredientList = self._step(
            run_id, deadline, "ingredient", "ingredient", 1, request,
            lambda: run_ingredient_agent(request, llm, self.cache, bypass_cache=bypass_cache),
            lambda r: f"{len(r.items)} items from {r.cache.hits + r.cache.misses} dishes "
            f"({r.cache.hits} cached)",
        )  # fmt: skip
        self.store.update_run(run_id, ingredient_output=_json(ing))
        if not ing.items:
            self._stop(run_id, ing, None, config, attempt=1, deadline=deadline)
            return

        price: PriceAgentResult = self._step(
            run_id, deadline, "price", "price", 1, {"items": ing.items},
            lambda: price_items(ing.items, llm, self.source, config, as_of, self.stores),
            lambda r: f"priced {len(ing.items)} items at {len(self.stores)} stores, "
            f"{len(r.flags)} flags, {len(r.downgrades)} downgrades",
        )  # fmt: skip
        self.store.update_run(run_id, price_output=_json(price))
        if self._stop(run_id, ing, price, config, attempt=1, deadline=deadline):
            return

        def plan(allow_loopback: bool):
            return lambda: run_planner(
                price.items, llm, config, as_of, self.stores, allow_loopback=allow_loopback
            )

        planner = self._step(
            run_id, deadline, "planner", "planner", 1, {"items": price.items, "as_of": as_of},
            plan(True), lambda r: r.summary_line,
        )  # fmt: skip
        self.store.update_run(run_id, planner_output=_json(planner))

        if planner.recheck_items:
            hints = {r.item_id: r.hint for r in planner.recheck_items}
            subset = [i for i in ing.items if i.item_id in hints]
            if subset:
                self.store.update_run(run_id, loopback_used=True)
                second = self._step(
                    run_id, deadline, "reprice", "price", 2, {"items": subset, "hints": hints},
                    lambda: reprice(subset, hints, llm, self.source, config, as_of, self.stores),
                    lambda r: f"re-priced {len(subset)} item(s) using the planner's hints",
                )  # fmt: skip
                price = merge_price_results(price, second)
                self.store.update_run(run_id, price_output=_json(price))
                if self._stop(run_id, ing, price, config, attempt=2, deadline=deadline):
                    return
                planner = self._step(
                    run_id, deadline, "planner_2", "planner", 2,
                    {"items": price.items, "as_of": as_of}, plan(False), lambda r: r.summary_line,
                )  # fmt: skip
                self.store.update_run(run_id, planner_output=_json(planner))
        self._finish(run_id, "completed", deadline)

    # ---- steps ----------------------------------------------------------------------------

    def _step(self, run_id, deadline, name, agent, attempt, input_obj, fn, describe):
        deadline.check()
        seq = self.store.start_step(run_id, name, agent, attempt, _json_input(input_obj))
        started = self._clock()
        try:
            result = fn()
        except Exception as exc:
            self.store.finish_step(
                run_id, seq, status="failed", error=_public_error(exc),
                latency_ms=round((self._clock() - started) * 1000),
            )  # fmt: skip
            raise
        self.store.finish_step(
            run_id, seq, status="completed", output=_json(result),
            tool_calls=[asdict(t) for t in getattr(result, "tool_calls", [])],
            usage=result.usage, model=result.model or None, fell_back=result.fell_back,
            latency_ms=round((self._clock() - started) * 1000), summary=describe(result),
        )  # fmt: skip
        return result  # the deadline is checked before the next step, so this output is kept

    def _stop(self, run_id, ing, price, config, attempt, deadline) -> bool:
        """The stop check. On a stop: record a 'stop' step and end the run as stopped."""
        rows = price.items if price else []
        cov = coverage(rows, self.stores)
        stop, reasons = should_stop(cov, config)
        if not stop:
            return False
        total = cov.total_items
        nowhere = total and len(cov.unmatched_everywhere) / total > config.stop_not_found_pct
        reason = (
            "no_items" if not ing.items else ("too_many_unmatched" if nowhere else "not_comparable")
        )
        if not ing.items:
            message = (
                "Your meals only need pantry staples, so there is nothing to price. Turn on "
                "'include pantry staples' if you want them listed."
            )
        else:
            message = (
                "We couldn't match enough of your list to compare the stores: "
                + "; ".join(reasons)
                + "."
                + _LIST_NOTE
            )
        details = _stop_details(ing, price, cov, reasons)
        seq = self.store.start_step(run_id, "stop", None, attempt, {"reasons": reasons})
        self.store.finish_step(
            run_id, seq, status="completed", output=details, latency_ms=0,
            summary=f"stopped: {reason}",
        )  # fmt: skip
        self._finish(run_id, "stopped", deadline, stop_reason=reason, user_message=message)
        return True

    # ---- ending a run ---------------------------------------------------------------------

    def _finish(self, run_id: str, status: str, deadline: Deadline, **fields: Any) -> None:
        steps = [s for s in self.store.get_steps(run_id)]
        models: list[str] = []
        for s in steps:
            if s.model and s.model not in models:
                models.append(s.model)
        usage = _sum(steps)
        self.store.update_run(
            run_id, status=status, models_used=models, fell_back=any(s.fell_back for s in steps),
            requests=usage.requests, tokens_in=usage.input_tokens, tokens_out=usage.output_tokens,
            thinking_tokens=usage.thinking_tokens, latency_ms=deadline.elapsed_ms(),
            completed_at=datetime.now(UTC), **fields,
        )  # fmt: skip

    def _fail(self, run_id: str, exc: Exception, deadline: Deadline) -> None:
        reason, message = classify_failure(exc)
        if self.store.get_run(run_id).ingredient_output is not None:
            message += _LIST_NOTE
        try:
            self._finish(run_id, "failed", deadline, failure_reason=reason, user_message=message)
        except Exception:
            log.exception("could not record the failure of run %s", run_id)


def _json_input(obj: Any) -> Any:
    return to_jsonable_python(obj)  # models, dates and Decimals alike


def _sum(steps: list[StepRecord]) -> Usage:
    total = Usage()
    for s in steps:
        total += Usage(s.requests, s.tokens_in, s.tokens_out, s.thinking_tokens)
    return total


def _stop_details(ing: IngredientList, price: PriceAgentResult | None, cov, reasons) -> dict:
    """Which items could not be compared, and why (the model's own reason for each store)."""
    failed = []
    for item in ing.items:
        if item.item_id in cov.common_items:
            continue
        stores: dict[str, Any] = {}
        for store in cov.per_store:
            row = next(
                (
                    r
                    for r in (price.items if price else [])
                    if r.item_id == item.item_id and r.store == store
                ),
                None,
            )
            stores[store.value] = {
                "match_tier": row.match_tier if row else "not_found",
                "product": row.product_name if row else None,
                "reason": (price.reasons.get(item.item_id, {}).get(store.value) if price else None),
            }
        failed.append(
            {
                "item_id": item.item_id,
                "name": item.name,
                "quantity": str(item.quantity.normalize()),
                "unit": item.unit,
                "found_nowhere": item.item_id in cov.unmatched_everywhere,
                "stores": stores,
            }
        )
    return {
        "reasons": reasons,
        "ingredient_count": len(ing.items),
        "comparable_count": len(cov.common_items),
        "failed_items": failed,
    }
