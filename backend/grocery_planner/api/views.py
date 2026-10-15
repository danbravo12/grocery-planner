"""What the public may see of a run. Built only from fields known to be safe; anything not named
here (visitor and IP data, reservations, Langfuse ids, raw errors, the actual charge) cannot
reach a response."""

from typing import Any

from grocery_planner.orchestrator.models import RunRecord, StepRecord

PLANNER_FIELDS = (
    "recommendation", "store", "split_assignment", "savings", "summary_line", "reasoning",
    "confidence", "overridden", "override_reason", "tips", "recheck_items", "assumptions",
    "flags", "model", "fell_back", "prompt_version",
)  # fmt: skip
PRODUCT_FIELDS = (
    "product_id", "product_name", "brand", "brand_label", "pack_size", "pack_unit", "price",
    "regular_price", "unit_price", "on_special", "special_end_date", "packs_needed",
    "purchased_qty", "leftover_qty", "total_cost", "regular_total_cost", "match_tier",
    "match_confidence", "price_timestamp", "data_source",
)  # fmt: skip


def _pick(source: dict[str, Any] | None, fields: tuple[str, ...]) -> dict[str, Any]:
    return {f: source[f] for f in fields if source and f in source}


def _comparison(run: RunRecord) -> list[dict[str, Any]]:
    price = run.price_output or {}
    reasons = price.get("reasons", {})
    listing = {i["item_id"]: i for i in (run.ingredient_output or {}).get("items", [])}
    rows: dict[str, dict[str, Any]] = {}
    for r in price.get("items", []):
        entry = rows.setdefault(
            r["item_id"],
            {
                "item_id": r["item_id"],
                "name": r["item_name"],
                "quantity": (listing.get(r["item_id"]) or {}).get("quantity"),
                "unit": (listing.get(r["item_id"]) or {}).get("unit"),
                "stores": {},
            },
        )
        entry["stores"][r["store"]] = {
            **_pick(r, PRODUCT_FIELDS),
            "reason": reasons.get(r["item_id"], {}).get(r["store"]),
        }
    return list(rows.values())


def _recommendation(run: RunRecord) -> dict[str, Any] | None:
    planner = run.planner_output
    if not planner:
        return None
    verdict = planner.get("rule_verdict") or {}
    return {
        **_pick(planner, PLANNER_FIELDS),
        "rule_verdict": {
            **_pick(
                verdict,
                ("kind", "store", "totals", "gap", "threshold", "split_savings", "condition"),
            ),
        },
    }


def run_view(run: RunRecord) -> dict[str, Any]:
    params = run.parameters or {}
    return {
        "id": run.id,
        "status": run.status,
        "stop_reason": run.stop_reason,
        "failure_reason": run.failure_reason,
        "message": run.user_message,
        "created_at": run.created_at.isoformat(),
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "request": run.input,
        "parameters": {
            k: params[k] for k in ("config", "as_of", "stores", "prompt_versions") if k in params
        },
        "ingredient_list": (
            {
                "items": run.ingredient_output.get("items", []),
                "assumptions": run.ingredient_output.get("assumptions", []),
            }
            if run.ingredient_output
            else None
        ),
        "comparison": _comparison(run),
        "recommendation": _recommendation(run),
        "totals": {
            "requests": run.requests,
            "request_budget": run.estimated_requests,
            "tokens_in": run.tokens_in,
            "tokens_out": run.tokens_out,
            "thinking_tokens": run.thinking_tokens,
            "latency_ms": run.latency_ms,
            "models": run.models_used,
            "fell_back": run.fell_back,
            "loopback_used": run.loopback_used,
            "would_cost_usd": str(run.would_cost_usd),
        },
    }


def _describe(value: Any) -> Any:
    """A short stand-in for a step's input: scalars as they are, containers by size."""
    if isinstance(value, dict):
        return {k: _describe(v) for k, v in value.items()} if len(value) <= 8 else len(value)
    if isinstance(value, list):
        return f"{len(value)} entries"
    if isinstance(value, str) and len(value) > 120:
        return value[:117] + "..."
    return value


def _output_summary(step: StepRecord) -> Any:
    out = step.output
    if not isinstance(out, dict):
        return None
    if step.name == "ingredient":
        cache = out.get("cache") or {}
        return {
            "items": len(out.get("items", [])),
            "cache_hits": cache.get("hits"),
            "cache_misses": cache.get("misses"),
            "assumptions": out.get("assumptions", []),
        }
    if step.name in ("price", "reprice"):
        return {
            "rows": len(out.get("items", [])),
            "rounds": out.get("rounds"),
            "flags": [{"item_id": f["item_id"], "kind": f["kind"]} for f in out.get("flags", [])],
            "downgrades": [
                {
                    "item_id": d["item_id"],
                    "kind": d.get("kind"),
                    "from": d.get("from_tier"),
                    "to": d["to_tier"],
                    "reason": d.get("reason"),
                }
                for d in out.get("downgrades", [])
            ],
        }
    if step.name in ("planner", "planner_2"):
        return {
            k: out.get(k)
            for k in ("recommendation", "store", "overridden", "override_reason", "confidence")
        }
    if step.name == "stop":
        return out  # reasons, counts and the failed items: already written for the public
    return None


def step_view(step: StepRecord) -> dict[str, Any]:
    return {
        "seq": step.seq,
        "name": step.name,
        "agent": step.agent,
        "attempt": step.attempt,
        "status": step.status,
        "summary": step.summary,
        "input": _describe(step.input),
        "output": _output_summary(step),
        "tool_calls": [
            {
                "name": t.get("name"),
                "args": t.get("args"),
                "result": t.get("result_summary"),
                "latency_ms": t.get("latency_ms"),
                "error": t.get("error"),
            }
            for t in step.tool_calls
        ],
        "requests": step.requests,
        "tokens_in": step.tokens_in,
        "tokens_out": step.tokens_out,
        "thinking_tokens": step.thinking_tokens,
        "latency_ms": step.latency_ms,
        "model": step.model,
        "fell_back": step.fell_back,
        "error": step.error,  # a class name only
        "started_at": step.started_at.isoformat(),
        "finished_at": step.finished_at.isoformat() if step.finished_at else None,
    }
