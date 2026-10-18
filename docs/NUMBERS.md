# Numbers

Every figure in the README comes from this page. Each one says where it was measured and when. Nothing here is an estimate unless it says so.

**Measured on 2026-10-19 (Sydney)**, which is 2026-10-18 22:11 UTC by the database clock. Nothing was measured before that day except where a date is given.

**Read this first.** The service was deployed on 2026-10-18 and the demo has not been promoted. The database is shared by development and production and has no marker that separates a visitor's run from the author's, so treat the 20 completed runs of the last 7 days below as test and smoke runs (the author's own live checks account for the latest ones), not as user traffic. Gemini's speed also varies a lot from day to day (the same request took 11 s on one day and 45 s on another), so the latency figures describe those 20 runs and not a promise.

## 1. Runs (Supabase `runs` and `run_steps`)

Source: the project's Supabase database, queried on 2026-10-19 through the SQL API. Window: `status = 'completed'` and `created_at > now() - interval '7 days'`. First run in the window 2026-10-14 08:13 UTC, last 2026-10-18 22:08 UTC.

| Figure | Value | How |
| --- | --- | --- |
| Completed runs in the window | **20** (all runs in the table; none failed or stopped) | `count(*)` |
| End-to-end latency, median | **10.2 s** | `percentile_cont(0.5) within group (order by latency_ms)` |
| End-to-end latency, p90 | **31.4 s** | `percentile_cont(0.9) ...` |
| End-to-end latency, min / max | 3.8 s / 45.9 s | `min`, `max` |
| Model requests per run, median | **3** (min 2, max 4, mean 2.95) | `percentile_cont(0.5) within group (order by requests)` |
| Runs by requests | 2: 6 runs, 3: 9 runs, 4: 5 runs | `count(*) filter (where requests = n)` |
| Runs that used the fallback model | **0 of 20 (0%)** | `count(*) filter (where fell_back)` |
| Runs that used the loop-back | 0 of 20 | `count(*) filter (where loopback_used)` |
| Would-be cost per run (paid-tier price), median | **$0.0028** (min $0.0013, max $0.0070) | `percentile_cont(0.5)` over the 17 runs with `would_cost_usd > 0`; the 3 oldest runs predate the cost column (migration 006) |
| Would-be cost of all 17 priced runs | $0.0511 | `sum(would_cost_usd)` |
| Ingredient step: dishes served from the cache | **64.7%** (33 hits, 18 misses) | `sum` of `output->'cache'->>'hits'` and `'misses'` over the 20 ingredient steps |
| Ingredient step: runs with no model call at all | 11 of 20 (55.0%) | ingredient steps with `requests = 0` |

The cache rate is high because the same example dishes were run again and again while testing; real traffic would see a lower rate.

The would-be cost is what the requests would cost on Google's paid tier (prices in `backend/grocery_planner/tracing/pricing.py`, checked 2026-10-14). The free tier actually charges nothing.

### Latency per model call, by agent (from `run_steps`, same window)

Source: `run_steps` joined to `runs`, completed steps with `requests > 0`, per-call latency approximated as `latency_ms / requests`. Cached ingredient steps are left out. Measured 2026-10-18, a few hours before the table above.

| Agent | Steps | Requests | p50 per call | p95 per call | max per call |
| --- | ---: | ---: | ---: | ---: | ---: |
| ingredient | 8 | 8 | 2.5 s | 3.5 s | 3.6 s |
| price | 18 | 20 | 4.3 s | 12.2 s | 18.6 s |
| planner | 18 | 25 | 2.9 s | 13.9 s | 15.2 s |

The p95 figures are inflated for the price and planner steps: a call that hung until the old 20 s timeout and was retried counts in `latency_ms`, so the p95 partly measures the timeout. The per-agent timeouts (ingredient 12 s, price 25 s, planner 12 s) are a first estimate to be revisited from successful-request latencies in Langfuse after a week of traffic (DECISIONS.md, step 15b).

## 2. Evals

| Figure | Value | Source |
| --- | --- | --- |
| Cases | **39**: ingredient 12, price 12, planner 11, end to end 4 | `grep -c '^- id:' backend/evals/cases/*.yaml`, 2026-10-19 |
| Approved baseline | pass rate **100%** for ingredient, price, planner and end to end; 3 repeats; model `gemini-3.1-flash-lite`; anchor 2026-10-14; approved by Daniel 2026-10-16 | `backend/evals/baseline.json` |
| Floors in the gate | ingredient 85%, price 85%, planner 90%; also fails if an agent is more than 10 points under its baseline or if any answer came from a fallback model; no floor for end to end | `backend/evals/report.py`, `baseline.json` |
| Latest CI run | **39 of 39 passed** (ingredient 12/12, price 12/12, planner 11/11, end to end 4/4), 0 not scored, gate PASS, **1 repeat** | GitHub Actions "Evals", run 39116871854 on commit `45685b5`, started 2026-10-18 09:49 UTC |
| Same run, usage | 48 model calls, 0 repairs, 4 transport retries (never failures), 0 downgrades, 0 fallbacks; 120,993 input and 23,128 output tokens; would-be cost $0.0649; average 7.2 s per case (end to end 16.3 s) | the run's job summary |
| The prompt-injection case | `ing-prompt-injection-dish-name` passed in that run (and in the manual run 39116567253 of the same day) | the run logs |
| Model comparison, 3 repeats each (3.1 vs 3.5) | 3.1: ingredient 100%, price 100%, planner 100%, end to end 83.3% (before the case was replaced; 100% after, 12 of 12 runs); 3.5: ingredient 94.4%, price 100%, planner 100%, end to end 91.7%. Repairs 0 vs 8; would-be cost $0.19 vs $0.31; average case latency 6.3 s vs 7.1 s | DECISIONS.md, step 11c ("Clean re-run", run on 2026-10-15) and step 11d |

The CI gate runs once per push that touches the agents, prompts, model layer, tools, evals, fixtures or seed, and on demand. Temperature stays at 0.1, so a case can pass in one run and fail in the next; that is why the baseline came from three repeats.

## 3. Tests

Counted on 2026-10-19.

| Figure | Value | How |
| --- | --- | --- |
| Backend tests | **571** collected: 527 unit (offline), 28 integration (a real Postgres), 16 live (the real Gemini API, only with `-m live`) | `pytest --collect-only -q`, with `-m "not integration and not live"`, `-m integration`, `-m live` |
| Backend, a normal local run | 555 passed, 16 skipped (the live ones) | `pytest -q` with the database reachable |
| Frontend unit tests | **60** (Vitest, 8 files) | `npx vitest run` |
| Frontend end to end | 2 (Playwright, every `/api` call mocked) | `grep -c '^test(' frontend/e2e/smoke.spec.ts` |
| CI | lint and unit tests, integration tests (a Postgres service container), frontend (typecheck, lint, tests, types-are-current check, build, Playwright): all green on commit `7da888e` (`da71a08` before the history rewrite) | GitHub Actions, 2026-10-19 |
| Size | 8,079 lines of backend code, 8,406 lines of backend tests, 1,766 of eval code, 4,065 of frontend; 35 commits | `wc -l` and `git rev-list --count HEAD` on `7da888e` (`da71a08` before the history rewrite) |

## 4. The deployed service

Measured on 2026-10-18 against `https://grocery-planner-99862655427.australia-southeast1.run.app` (Cloud Run, Sydney). Details and method in [DEPLOY.md](DEPLOY.md).

| Figure | Value | Source |
| --- | --- | --- |
| Cold start | **6.6 s** to the first byte (0.65 s of it connecting); **one sample** | `curl -w` after the instance had shut down (04:54 UTC) |
| Warm requests | 0.2 to 0.5 s from the author's machine | `curl -w` |
| Catalogue search, from Cloud Run | **p50 13 ms, p95 16 ms**, max 30 ms, over 100 searches | a one-off Cloud Run job running `grocery_planner.tools.bench_search`, since deleted |
| Catalogue search, from a laptop | median 27 ms, p95 32 ms (50 calls, direct connection) | DECISIONS.md, step 3 |
| A live preset run (small, ingredients cached) | 5.9 s, 3 requests | the run `affbe09c-...`, 2026-10-19 |
| Two earlier live runs | 45.9 s (4 requests) and 44.9 s (3 requests, 14 items); the same 14-item request from a laptop took 44.3 s, so the time was Gemini's | DEPLOY.md |
| Monthly cost | about $0 to $2 at demo traffic; about $50 to $65 if the instance never idled. The Sydney (Tier 2) price list is not on Google's public page, so this is an estimate from the Tier 1 prices | DEPLOY.md |

## 5. Daily capacity

| Figure | Value | How |
| --- | --- | --- |
| Free-tier requests a day | 500, as shown in AI Studio for this project's models (checked by Daniel on 2026-10-14); Google can change it | Daniel's check of AI Studio; design doc, Guardrails |
| Daily limit and admission limit | 350 (70%) and **298** (350 less a 15% buffer) requests per Pacific day | `backend/grocery_planner/config.py`, pinned by a test |
| Median requests per run | 3 (section 1) | |
| **Runs a day at the median** | **298 / 3 = 99** (101 at the mean of 2.95) | arithmetic on the two lines above |
| Runs a day if every run used its worst case | 298 / 9 = **33** (a run reserves 9 requests up front: 1 + 2 + 3 + 1 + 2) | arithmetic |
| Per visitor and per IP | 5 runs a visitor and 20 an IP per Pacific day | `GuardrailConfig` |

The number of runs a day is a ceiling set by the free tier, not by the server. In practice the per-visitor cap of 5 means about 20 visitors a day could use the whole median-sized budget.

## 6. The data behind the demo

Counted on 2026-10-19 in the Supabase table `price_catalogue`: **346 products** for **103 ingredients** at two stores, 3 on special. The prices are seeded and fictional.
