"""Run one case against the real agents and return what happened (before any checking)."""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from evals.builders import build_rows
from evals.cases import Case
from evals.harness import ANCHOR_ISO, Meter, scrub
from grocery_planner.agents.ingredient import run_ingredient_agent
from grocery_planner.agents.planner import PlannerFacts, PlannerResult, build_facts, run_planner
from grocery_planner.agents.price import PriceAgentResult, price_items
from grocery_planner.config import ACTIVE_STORES
from grocery_planner.db.dish_cache import InMemoryDishCache
from grocery_planner.domain import PlannerConfig, PricedItem
from grocery_planner.domain.ingredients import IngredientItem, IngredientList
from grocery_planner.domain.request import PlanRequest
from grocery_planner.llm import ModelHooks
from grocery_planner.llm.types import LLMError, SchemaValidationFailed
from grocery_planner.orchestrator import InMemoryRunStore, Runner, RunnerSettings
from grocery_planner.orchestrator.models import RunRecord, StepRecord
from grocery_planner.tools.price_lookup import SeededFilePriceSource
from grocery_planner.tools.vocabulary import ingredient_vocabulary

ANCHOR = date.fromisoformat(ANCHOR_ISO)
# Builds a client that reports into the meter. Real runs use harness.make_client; tests inject
# a fake.
LLMFactory = Callable[[Meter, ModelHooks | None], Any]


@dataclass
class IngredientSubject:
    request: PlanRequest
    result: IngredientList


@dataclass
class PriceSubject:
    items: list[IngredientItem]
    result: PriceAgentResult
    source: SeededFilePriceSource
    config: PlannerConfig


@dataclass
class PlannerSubject:
    rows: list[PricedItem]
    facts: PlannerFacts
    result: PlannerResult
    config: PlannerConfig


@dataclass
class E2ESubject:
    request: PlanRequest
    run: RunRecord
    steps: list[StepRecord]


Subject = IngredientSubject | PriceSubject | PlannerSubject | E2ESubject


@dataclass
class Execution:
    subject: Subject | None
    error: str | None = None
    error_kind: str | None = None  # schema | model | internal
    latency_s: float = 0.0
    extra_metrics: dict[str, Any] = field(default_factory=dict)


def _ingredient(case: Case, make_llm: LLMFactory, meter: Meter) -> Subject:
    request = PlanRequest.model_validate(case.input)
    result = run_ingredient_agent(
        request, make_llm(meter, None), InMemoryDishCache(), bypass_cache=True
    )
    return IngredientSubject(request, result)


def price_inputs(spec: list[dict[str, Any]]) -> list[IngredientItem]:
    vocab = ingredient_vocabulary()
    items = []
    for n, raw in enumerate(spec, start=1):
        entry = vocab.get(raw["name"])
        items.append(
            IngredientItem(
                item_id=f"i{n}",
                name=raw["name"],
                quantity=Decimal(str(raw["quantity"])),
                unit=raw["unit"],
                category=entry.category if entry else "other",
                meals=["eval"],
                brand=raw.get("brand"),
                pantry_staple=False,
                in_vocabulary=entry is not None,
            )
        )
    return items


def _price(case: Case, make_llm: LLMFactory, meter: Meter) -> Subject:
    items = price_inputs(case.input["items"])
    config = PlannerConfig.model_validate(case.input.get("config", {}))
    source = SeededFilePriceSource(anchor=ANCHOR)
    result = price_items(items, make_llm(meter, None), source, config, ANCHOR, ACTIVE_STORES)
    return PriceSubject(items, result, source, config)


def _planner(case: Case, make_llm: LLMFactory, meter: Meter) -> Subject:
    rows, as_of = build_rows(case.input["build"])
    config = PlannerConfig.model_validate(case.input.get("config", {}))
    facts = build_facts(rows, ACTIVE_STORES, config, as_of)
    result = run_planner(rows, make_llm(meter, None), config, as_of, ACTIVE_STORES)
    return PlannerSubject(rows, facts, result, config)


def _e2e(case: Case, make_llm: LLMFactory, meter: Meter) -> Subject:
    from grocery_planner.tracing import tracer_from_env

    raw = dict(case.input)
    config = PlannerConfig.model_validate(raw.pop("parameters", {}))
    request = PlanRequest.model_validate(raw)
    runner = Runner(
        InMemoryRunStore(),
        lambda hooks: make_llm(meter, hooks),
        InMemoryDishCache(),
        SeededFilePriceSource(anchor=ANCHOR),
        settings=RunnerSettings(deadline_seconds=900.0),  # queueing behind the 12/min limiter
        tracer=tracer_from_env(),  # off without Langfuse keys; traces are tagged env=eval
        app_env="eval",
    )
    run_id = runner.create_run(request, config, ANCHOR, bypass_cache=True)
    run = runner.run_plan(run_id)
    return E2ESubject(request, run, list(runner.store.get_steps(run_id)))


RUNNERS: dict[str, Callable[[Case, LLMFactory, Meter], Subject]] = {
    "ingredient": _ingredient,
    "price": _price,
    "planner": _planner,
    "e2e": _e2e,
}


def execute(case: Case, make_llm: LLMFactory, meter: Meter) -> Execution:
    start = time.perf_counter()
    try:
        subject = RUNNERS[case.agent](case, make_llm, meter)
        return Execution(subject, latency_s=time.perf_counter() - start)
    except SchemaValidationFailed as exc:
        kind, msg = "schema", f"SchemaValidationFailed: {'; '.join(exc.errors)[:300]}"
    except LLMError as exc:
        kind, msg = "model", f"{type(exc).__name__}: {exc}"[:300]
    except Exception as exc:  # a bug in the case or the agent: report it, keep going
        kind, msg = "internal", f"{type(exc).__name__}: {exc}"[:300]
    return Execution(None, scrub(msg), kind, time.perf_counter() - start)
