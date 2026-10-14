"""Scoring, the baseline gate and the markdown summary. Pure functions over result dicts."""

import json
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from evals.cases import AGENTS

BASELINE_PATH = Path(__file__).parent / "baseline.json"
# Absolute floors: a run below these fails whatever the baseline says. The end-to-end cases have
# no floor of their own: with 3 cases, one failure is a 33-point drop below a 100% baseline.
FLOORS: dict[str, float] = {"ingredient": 0.85, "price": 0.85, "planner": 0.90}
MAX_DROP = 0.10  # fail when an agent falls more than 10 points below the baseline
_EPS = 1e-9


@dataclass
class CaseResult:
    id: str
    agent: str
    repeat: int
    passed: bool
    error: str | None
    error_kind: str | None
    latency_s: float
    outcomes: list[dict[str, Any]]  # {name, passed, detail}
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def case_checks(result: dict[str, Any]) -> dict[str, bool]:
    """check name -> passed (all instances of that check must pass)."""
    by_name: dict[str, bool] = {}
    for o in result["outcomes"]:
        by_name[o["name"]] = by_name.get(o["name"], True) and o["passed"]
    return by_name


def summarize(results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per agent: pass rates (overall, per repeat, per check) and usage totals."""
    summary: dict[str, dict[str, Any]] = {}
    for agent in AGENTS:
        rs = [r for r in results if r["agent"] == agent]
        if not rs:
            continue
        repeats = sorted({r["repeat"] for r in rs})
        per_repeat = [
            sum(r["passed"] for r in rs if r["repeat"] == k)
            / sum(1 for r in rs if r["repeat"] == k)
            for k in repeats
        ]
        checks: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for r in rs:
            for name, ok in case_checks(r).items():
                checks[name][0] += int(ok)
                checks[name][1] += 1
        errors: dict[str, int] = defaultdict(int)
        for r in rs:
            if r["error_kind"]:
                errors[r["error_kind"]] += 1

        def total(key: str, rs=rs) -> int:
            return sum(r["metrics"].get(key, 0) for r in rs)

        summary[agent] = {
            "cases": len({r["id"] for r in rs}),
            "runs": len(rs),
            "passed": sum(r["passed"] for r in rs),
            "pass_rate": sum(r["passed"] for r in rs) / len(rs),
            "repeat_rates": per_repeat,
            "rate_mean": sum(per_repeat) / len(per_repeat),
            "rate_min": min(per_repeat),
            "checks": {
                n: {"passed": p, "total": t, "rate": p / t} for n, (p, t) in sorted(checks.items())
            },
            "requests": total("requests"),
            "input_tokens": total("input_tokens"),
            "output_tokens": total("output_tokens"),
            "thinking_tokens": total("thinking_tokens"),
            "would_cost_usd": float(
                sum(Decimal(str(r["metrics"].get("would_cost_usd", 0))) for r in rs)
            ),
            "downgrades": total("downgrades"),
            "repairs": total("repairs"),
            "fell_back": sum(bool(r["metrics"].get("fell_back")) for r in rs),
            "errors": dict(errors),
            "latency_avg_s": sum(r["latency_s"] for r in rs) / len(rs),
            "latency_max_s": max(r["latency_s"] for r in rs),
        }
    return summary


def load_baseline(path: Path = BASELINE_PATH) -> dict[str, Any] | None:
    return json.loads(path.read_text()) if path.exists() else None


def baseline_from(summary: dict[str, dict[str, Any]], meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "draft",  # change to "approved" once the expected values are reviewed
        "recorded": meta["timestamp"],
        "model": meta["model"],
        "repeat": meta["repeat"],
        "pass_rates": {a: round(s["pass_rate"], 4) for a, s in summary.items()},
    }


def gate(
    summary: dict[str, dict[str, Any]],
    baseline: dict[str, Any] | None,
    floors: dict[str, float] = FLOORS,
    max_drop: float = MAX_DROP,
) -> list[str]:
    """Reasons the run fails; an empty list means it passes. Only agents that ran are judged."""
    failures = []
    for agent, s in summary.items():
        rate = s["pass_rate"]
        if agent in floors and rate < floors[agent] - _EPS:
            failures.append(f"{agent}: pass rate {rate:.1%} is below the {floors[agent]:.0%} floor")
        base = (baseline or {}).get("pass_rates", {}).get(agent)
        if base is not None and base - rate > max_drop + _EPS:
            failures.append(
                f"{agent}: pass rate {rate:.1%} is {(base - rate) * 100:.1f} points below the "
                f"baseline {base:.1%} (more than {max_drop * 100:.0f})"
            )
        if s["fell_back"]:
            failures.append(
                f"{agent}: {s['fell_back']} case run(s) were answered by a fallback model"
            )
    return failures


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def render_markdown(
    meta: dict[str, Any],
    summary: dict[str, dict[str, Any]],
    failures: list[str],
    baseline: dict[str, Any] | None,
    results: list[dict[str, Any]],
) -> str:
    repeat = meta["repeat"]
    base = (baseline or {}).get("pass_rates", {})
    lines = [
        f"# Eval results: {meta['model']}",
        "",
        f"Run {meta['timestamp']} · {meta['cases']} cases × {repeat} repeat(s) · APP_ENV=eval · "
        f"as_of {meta['as_of']} · fallback model off · deterministic checks only (no LLM judge)",
        "",
        "## Scores",
        "",
        "| Agent | Cases | Passed | Pass rate | "
        + ("Mean / min over repeats | " if repeat > 1 else "")
        + "Baseline | Floor |",
        "| --- | ---: | ---: | ---: | " + ("---: | " if repeat > 1 else "") + "---: | ---: |",
    ]
    for agent, s in summary.items():
        floor = FLOORS.get(agent)
        lines.append(
            f"| {agent} | {s['cases']} | {s['passed']}/{s['runs']} | {_pct(s['pass_rate'])} | "
            + (f"{_pct(s['rate_mean'])} / {_pct(s['rate_min'])} | " if repeat > 1 else "")
            + f"{_pct(base[agent]) if agent in base else '-'} | "
            f"{_pct(floor) if floor else '-'} |"
        )
    if baseline and baseline.get("status") != "approved":
        lines += ["", f"_Baseline status: {baseline.get('status')}. Expected values are a draft._"]
    lines += ["", "## Gate", ""]
    lines += [f"- FAIL: {f}" for f in failures] or [
        "- PASS: every agent is within the baseline and above its floor."
    ]

    lines += ["", "## Pass rate per check", ""]
    for agent, s in summary.items():
        lines += [f"**{agent}**", "", "| Check | Passed | Rate |", "| --- | ---: | ---: |"]
        lines += [
            f"| {n} | {c['passed']}/{c['total']} | {_pct(c['rate'])} |"
            for n, c in s["checks"].items()
        ]
        lines.append("")

    lines += [
        "## Usage",
        "",
        "Would-be cost is what the requests would cost on Google's paid tier (the eval key "
        "itself is free tier).",
        "",
        "| Agent | Requests | Input tok | Output tok | Thinking tok | Would-be cost | "
        "Avg latency | Downgrades | Repairs | Fell back | Errors |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for agent, s in summary.items():
        errors = ", ".join(f"{k}: {v}" for k, v in s["errors"].items()) or "0"
        lines.append(
            f"| {agent} | {s['requests']} | {s['input_tokens']} | {s['output_tokens']} | "
            f"{s['thinking_tokens']} | ${s['would_cost_usd']:.4f} | {s['latency_avg_s']:.1f}s | "
            f"{s['downgrades']} | {s['repairs']} | {s['fell_back']} | {errors} |"
        )

    failed = [r for r in results if not r["passed"]]
    lines += ["", "## Failing cases", ""]
    if not failed:
        lines.append("None.")
    by_case: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in failed:
        by_case[r["id"]].append(r)
    for case_id, runs in by_case.items():
        total = sum(1 for r in results if r["id"] == case_id)
        lines.append(f"- **{case_id}** failed {len(runs)} of {total} run(s)")
        seen: set[str] = set()
        for r in runs:
            for o in r["outcomes"]:
                text = f"{o['name']}: {o['detail']}"
                if not o["passed"] and text not in seen:
                    seen.add(text)
                    lines.append(f"  - {text}")
    return "\n".join(lines) + "\n"
