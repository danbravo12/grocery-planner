"""Command line: run a full plan from a JSON request file and print the result.

    uv run python -m grocery_planner.cli run examples/request_1.json [--as-of 2026-10-12]
        [--bypass-cache] [--memory] [--seed]

By default runs are stored in Postgres (SUPABASE_DB_URL). --memory keeps everything in process
(seed catalogue file, no database). The output is rendered from the stored run, so it shows
what a run page would.
"""

import argparse
import json
import sys
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from grocery_planner.domain import PlannerConfig
from grocery_planner.domain.request import PlanRequest
from grocery_planner.orchestrator import InMemoryRunStore, Runner
from grocery_planner.orchestrator.models import RunRecord, StepRecord
from grocery_planner.tools.price_lookup import today_sydney

RunnerFactory = Callable[[argparse.Namespace, date], Runner]


def load_request(path: Path) -> tuple[PlanRequest, PlannerConfig]:
    raw = json.loads(path.read_text())
    parameters = raw.pop("parameters", {})
    return PlanRequest.model_validate(raw), PlannerConfig.model_validate(parameters)


def build_runner(args: argparse.Namespace, as_of: date) -> Runner:
    from grocery_planner.db.dish_cache import InMemoryDishCache
    from grocery_planner.guardrails import InMemoryDayBudget
    from grocery_planner.llm import GeminiClient
    from grocery_planner.llm.gemini import resolve_models
    from grocery_planner.tools.price_lookup import SeededFilePriceSource

    def llm_factory(hooks):
        return GeminiClient(
            on_request=hooks.on_request,
            before_request=hooks.before_request,
            fallback_allowed=hooks.fallback_allowed,
            on_quota_exhausted=hooks.on_quota_exhausted,
            start_generation=hooks.start_generation,
            on_tool_call=hooks.on_tool_call,
        )

    from grocery_planner.tracing import tracer_from_env

    primary, fallback = resolve_models()
    tracer = tracer_from_env()
    if args.memory:
        return Runner(
            InMemoryRunStore(),
            llm_factory,
            InMemoryDishCache(),
            SeededFilePriceSource(anchor=as_of),
            budget=InMemoryDayBudget(primary, fallback),
            tracer=tracer,
            primary_model=primary,
        )
    from grocery_planner.db import db_url
    from grocery_planner.db.dish_cache import PostgresDishCache
    from grocery_planner.guardrails import PostgresDayBudget
    from grocery_planner.orchestrator import PostgresRunStore
    from grocery_planner.tools.price_lookup import PostgresPriceSource

    if args.seed:
        from grocery_planner.db.seed import seed

        print(f"seeded {seed(db_url(), as_of)} products (anchor {as_of})")
    url = db_url()
    return Runner(
        PostgresRunStore(url),
        llm_factory,
        PostgresDishCache(url),
        PostgresPriceSource(url),
        budget=PostgresDayBudget(url, primary, fallback),
        tracer=tracer,
        primary_model=primary,
    )


def render_budget(budget) -> str:
    """The day's counters per model, and whether the service is available."""
    lines = ["BUDGET (requests, Pacific day)"]
    for u in budget.snapshot():
        stop = f"  STOPPED ({u.stop_reason})" if u.day_stopped else ""
        lines.append(
            f"  {u.model:<26} used {u.used:>3}  reserved {u.reserved:>2}  "
            f"admission limit {u.admission_limit}  daily limit {u.daily_limit}{stop}"
        )
    a = budget.availability()
    lines.append(
        f"  availability: {'available' if a.available else 'UNAVAILABLE (' + str(a.reason) + ')'}"
        f"   resets {a.reset_at_utc:%Y-%m-%d %H:%M} UTC / {a.reset_at_sydney:%Y-%m-%d %H:%M} Sydney"
    )
    return "\n".join(lines)


# ---- rendering (from the stored run, as plain data) ---------------------------------------------


def _money(value: Any) -> str:
    return "-" if value is None else f"${Decimal(str(value)):,.2f}"


def render(run: RunRecord, steps: list[StepRecord]) -> str:
    out: list[str] = []
    w = out.append
    w(f"Run {run.id}")
    w(
        f"status: {run.status}"
        + (
            f" ({run.failure_reason or run.stop_reason})"
            if run.failure_reason or run.stop_reason
            else ""
        )
        + f"   models: {', '.join(run.models_used) or '-'}   fell back: {run.fell_back}"
    )
    w(
        f"requests: {run.requests} of {run.estimated_requests} reserved   tokens: "
        f"{run.tokens_in} in / {run.tokens_out} out / {run.thinking_tokens} thinking   "
        f"time: {(run.latency_ms or 0) / 1000:.1f}s   loop-back used: {run.loopback_used}"
    )
    w(
        f"cost: ${run.cost_usd:.2f} charged (free tier)   would cost on the paid tier: "
        f"${run.would_cost_usd:.6f}"
    )

    ing = run.ingredient_output
    if ing:
        w("\nSHOPPING LIST")
        for i in ing["items"]:
            brand = f" [{i['brand']}]" if i.get("brand") else ""
            flags = f"  ! {'; '.join(i['flags'])}" if i["flags"] else ""
            w(
                f"  {i['item_id']:<4}{i['name']:<22}{Decimal(i['quantity']).normalize():>8f} "
                f"{i['unit']:<5}{i['category']:<9}{brand}{flags}"
            )
        for a in ing["assumptions"]:
            w(f"  - {a}")

    price = run.price_output
    if price:
        stores = sorted({r["store"] for r in price["items"]})
        w("\nPER-STORE COMPARISON")
        by_item: dict[str, dict[str, dict]] = {}
        for r in price["items"]:
            by_item.setdefault(r["item_id"], {})[r["store"]] = r
        names = {i["item_id"]: i["name"] for i in (ing or {}).get("items", [])}
        for item_id, rows in by_item.items():
            w(f"  {item_id} {names.get(item_id, '')}")
            for s in stores:
                r = rows[s]
                pick = r["product_name"] or "(no match)"
                special = f" special to {r['special_end_date']}" if r["on_special"] else ""
                cost = _money(r["total_cost"])
                w(f"      {s:<11}{pick:<42}{r['match_tier']:<11}{cost:>9}{special}")
        for f in price["flags"]:
            w(f"  flag [{f['kind']}] {f['item_id']}: {f['message']}")

    planner = run.planner_output
    if planner:
        v = planner["rule_verdict"]
        w("\nRECOMMENDATION")
        w(f"  {planner['summary_line']}")
        totals = ", ".join(f"{s} {_money(t)}" for s, t in v["totals"].items())
        w(f"  totals on common items: {totals}   threshold {_money(v['threshold'])}")
        w(
            f"  recommendation: {planner['recommendation']}"
            + (f" ({planner['store']})" if planner.get("store") else "")
            + f"   confidence: {planner['confidence']}"
            + (f"   OVERRIDE: {planner['override_reason']}" if planner["overridden"] else "")
        )
        w(f"  {planner['reasoning']}")
        for t in planner["tips"]:
            w(f"  tip {t['item_id']}: {t['text']}")
        for f in planner["flags"]:
            w(f"  flag [{f['kind']}] {f['message']}")
        for a in planner["assumptions"]:
            w(f"  assumption: {a}")

    if run.status in ("stopped", "failed"):
        w(f"\n{run.status.upper()}: {run.user_message}")
        stop = next((s for s in steps if s.name == "stop"), None)
        for f in (stop.output or {}).get("failed_items", []) if stop else []:
            per_store = "; ".join(
                f"{s}: {d['match_tier']} ({d['reason'] or 'no reason given'})"
                for s, d in f["stores"].items()
            )
            w(f"  - {f['name']}: {per_store}")

    w("\nSTEPS")
    header = ("#", "step", "status", "model", "req", "tokens in/out", "time")
    w(f"  {header[0]:<3}{header[1]:<11}{header[2]:<11}{header[3]:<24}{header[4]:>4}"
      f"{header[5]:>16}{header[6]:>8}")  # fmt: skip
    for s in steps:
        model = (s.model or "-") + (" (fallback)" if s.fell_back else "")
        tokens = f"{s.tokens_in}/{s.tokens_out}"
        w(
            f"  {s.seq:<3}{s.name:<11}{s.status:<11}{model:<24}{s.requests:>4}{tokens:>16}"
            f"{(s.latency_ms or 0) / 1000:>7.1f}s"
            + (f"   {s.summary}" if s.summary else "")
            + (f"   ERROR {s.error}" if s.error else "")
        )
    return "\n".join(out)


def main(argv: list[str] | None = None, runner_factory: RunnerFactory = build_runner) -> int:
    parser = argparse.ArgumentParser(prog="grocery_planner.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run a full plan from a JSON request file")
    run.add_argument("request", type=Path)
    run.add_argument(
        "--as-of", type=date.fromisoformat, default=None, help="YYYY-MM-DD (default: today, Sydney)"
    )
    run.add_argument("--bypass-cache", action="store_true", help="skip the dish cache")
    run.add_argument("--memory", action="store_true", help="in-process store, no database")
    run.add_argument("--seed", action="store_true", help="refresh the Postgres catalogue first")
    args = parser.parse_args(argv)

    as_of = args.as_of or today_sydney()
    request, config = load_request(args.request)
    runner = runner_factory(args, as_of)
    availability = runner.budget.availability() if hasattr(runner.budget, "availability") else None
    if availability is not None and not availability.available:
        print(availability.message)
        print(render_budget(runner.budget))
        return 3
    run_id = runner.create_run(request, config, as_of, bypass_cache=args.bypass_cache)
    print(f"run {run_id} created; running...", flush=True)
    record = runner.run_plan(run_id)
    print(render(record, runner.store.get_steps(run_id)))
    if record.langfuse_trace_id:
        url = runner.tracer.trace_url(record.langfuse_trace_id)
        print(f"\nTRACE {record.langfuse_trace_id}" + (f"\n  {url}" if url else ""))
    else:
        print("\n(tracing is off: no LANGFUSE keys)")
    if hasattr(runner.budget, "snapshot"):
        print("\n" + render_budget(runner.budget))
    return {"completed": 0, "stopped": 1}.get(record.status, 2)


if __name__ == "__main__":
    sys.exit(main())
