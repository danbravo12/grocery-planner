"""Run the evals.

    uv run python -m evals.run [--agent X] [--case ID] [--model M] [--repeat N]

Needs GEMINI_EVAL_API_KEY (never the demo's key). Writes evals/results/<timestamp>-<model>.json
and .md and prints the markdown. Exit codes: 0 passed; 1 an agent dropped more than 10 points
below evals/baseline.json or under its floor (ingredient 85%, price 85%, planner 90%), too many
runs were lost to infrastructure errors, or an answer came from a fallback model; 2 the
environment is not set up; 3 the eval key's daily quota ran out (the run stops there and
nothing is graded).
"""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from evals.cases import AGENTS, Case, load_cases
from evals.checks import check_names, evaluate
from evals.execute import ANCHOR, LLMFactory, execute
from evals.harness import (
    ANCHOR_ISO,
    EvalConfigError,
    EvalQuotaExhausted,
    Meter,
    make_client,
    prepare_environment,
    resolve_model,
)
from evals.report import (
    BASELINE_PATH,
    CaseResult,
    baseline_from,
    gate,
    load_baseline,
    render_markdown,
    summarize,
)

RESULTS_DIR = Path(__file__).parent / "results"


def run_case(case: Case, make_llm: LLMFactory, repeat: int) -> CaseResult:
    """Raises EvalQuotaExhausted when the daily quota is gone; the run stops there."""
    meter = Meter()
    execution = execute(case, make_llm, meter)
    subject = execution.subject
    kinds: dict[str, int] = {}
    if case.agent == "price" and subject is not None:
        found = [d.kind for d in subject.result.downgrades]
    elif case.agent == "e2e" and subject is not None:
        found = [d.get("kind", "") for d in (subject.run.price_output or {}).get("downgrades", [])]
    else:
        found = []
    for kind in found:
        kinds[kind or "unknown"] = kinds.get(kind or "unknown", 0) + 1
    infra = execution.error_kind == "infra"
    outcomes = [] if infra else evaluate(case, execution, meter)
    return CaseResult(
        id=case.id,
        agent=case.agent,
        repeat=repeat,
        passed=not infra and all(o.passed for o in outcomes),
        scored=not infra,
        error=execution.error,
        error_kind=execution.error_kind,
        latency_s=round(execution.latency_s, 2),
        outcomes=[{"name": o.name, "passed": o.passed, "detail": o.detail} for o in outcomes],
        metrics={
            "calls": meter.calls,
            "repairs": meter.repairs,
            "model_calls": meter.model_calls,
            "transport_retries": meter.transport_retries,
            "sends": meter.sends,
            "input_tokens": meter.input_tokens,
            "output_tokens": meter.output_tokens,
            "thinking_tokens": meter.thinking_tokens,
            "would_cost_usd": str(meter.would_cost_usd),
            "downgrades": len(found),
            "downgrade_kinds": kinds,
            "fell_back": meter.fell_back,
            "models": meter.models,
        },
    )


def validate_cases(cases: list[Case]) -> None:
    for c in cases:
        unknown = set(c.checks) - check_names(c.agent)
        if unknown:
            raise ValueError(f"{c.id}: unknown checks {sorted(unknown)}")


def run_all(
    cases: list[Case], make_llm: LLMFactory, repeat: int, progress=None
) -> tuple[list[CaseResult], str | None]:
    """Run every case `repeat` times. Returns the results and, if the daily quota ran out, the
    quota message (the results are then what finished before it; the case in flight is dropped)."""
    progress = progress or (lambda line: print(line, flush=True))
    results: list[CaseResult] = []
    total = len(cases) * repeat
    for k in range(1, repeat + 1):
        for case in cases:
            try:
                r = run_case(case, make_llm, k)
            except EvalQuotaExhausted as exc:
                progress(f"QUOTA: {exc}. Stopping after {len(results)} of {total} case runs.")
                return results, str(exc)
            results.append(r)
            failed = [o["name"] for o in r.outcomes if not o["passed"]]
            verdict = (
                "NOT SCORED (infrastructure: " + (r.error or "") + ")"
                if not r.scored
                else "PASS"
                if r.passed
                else "FAIL " + ", ".join(sorted(set(failed)))
            )
            m = r.metrics
            progress(
                f"[{len(results)}/{total}] {case.id} (repeat {k}): {verdict}  "
                f"{m['model_calls']} calls ({m['repairs']} repairs), "
                f"{m['transport_retries']} transport retries, {r.latency_s:.1f}s"
            )
    return results, None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m evals.run", description=__doc__)
    ap.add_argument("--agent", action="append", choices=AGENTS, help="only this agent (repeatable)")
    ap.add_argument("--case", action="append", help="only this case id (repeatable)")
    ap.add_argument("--model", help="model id (default GEMINI_MODEL, else the app default)")
    ap.add_argument("--repeat", type=int, default=1, help="run the whole set N times")
    ap.add_argument("--out-dir", type=Path, default=RESULTS_DIR)
    ap.add_argument("--list", action="store_true", help="list the selected cases and exit")
    ap.add_argument(
        "--write-baseline",
        action="store_true",
        help=f"write this run's pass rates to {BASELINE_PATH.name} as a draft",
    )
    args = ap.parse_args(argv)

    cases = load_cases()
    validate_cases(cases)
    if args.agent:
        cases = [c for c in cases if c.agent in args.agent]
    if args.case:
        cases = [c for c in cases if c.id in args.case]
    if args.list:
        for c in cases:
            print(f"{c.agent:<11}{c.id}")
        return 0
    if not cases or args.repeat < 1:
        print("no cases selected" if not cases else "--repeat must be at least 1", file=sys.stderr)
        return 2
    try:
        prepare_environment()
    except EvalConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    model = resolve_model(args.model)

    def make_llm(meter, hooks):
        return make_client(model, meter, hooks)

    print(f"evals: {len(cases)} cases x {args.repeat} on {model} (as_of {ANCHOR_ISO})", flush=True)
    results, quota = run_all(cases, make_llm, args.repeat)

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    meta = {
        "timestamp": stamp,
        "model": model,
        "repeat": args.repeat,
        "cases": len(cases),
        "as_of": ANCHOR.isoformat(),
        "agents": sorted({c.agent for c in cases}),
        "status": "quota" if quota else "complete",
        "quota_message": quota,
        "completed_runs": len(results),
        "planned_runs": len(cases) * args.repeat,
    }
    rows = [r.to_dict() for r in results]
    summary = summarize(rows)
    baseline = load_baseline()
    failures = [] if quota else gate(summary, baseline)  # a partial run is never graded
    markdown = render_markdown(meta, summary, failures, baseline, rows)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    base = args.out_dir / f"{stamp}-{model}"  # not with_suffix: the model id contains dots
    json_path, md_path = Path(f"{base}.json"), Path(f"{base}.md")
    json_path.write_text(
        json.dumps({"meta": meta, "summary": summary, "gate": failures, "cases": rows}, indent=1)
        + "\n"
    )
    md_path.write_text(markdown)
    print("\n" + markdown)
    print(f"wrote {json_path} and {md_path.name}")
    if args.write_baseline and not quota:
        BASELINE_PATH.write_text(json.dumps(baseline_from(summary, meta), indent=1) + "\n")
        print(f"wrote {BASELINE_PATH} (draft)")
    if quota:
        return 3  # not a pass and not a fail: out of quota, nothing graded
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
