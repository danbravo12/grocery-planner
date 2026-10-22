# Repository map

Every file tracked in git, grouped by folder, with what it does. Descriptions come from each file's own docstring or header comment. Generated files are marked. Last checked against the repository on 2026-10-22.

## How the pieces fit

```
frontend/          React form, progress, result, trace panel
   │ /api          same origin, SSE for progress
api/               FastAPI app, run queue, public views
   │
orchestrator/      ingredient → price → stop check → planner
   │               (at most one loop-back)
   ├── agents/     the three LLM agents
   ├── llm/        Gemini client (and a fake for tests)
   ├── guardrails/ daily request budget
   └── tracing/    Langfuse, step records
tools/             price lookup, planner maths, vocabulary
domain/            shared types
db/                migrations and seeded catalogue → Supabase

(Python packages live under backend/grocery_planner/.)
```

## Root

| File | Function |
| --- | --- |
| `README.md` | Front page: what the app does, how it works, headline numbers, links to the docs. |
| `LICENSE` | The project's licence. |
| `Dockerfile` | Multi-stage image built by Cloud Build: builds the frontend, then the Python backend that serves both from one origin. |
| `.env.example` | Every environment setting with comments. Copy to `backend/.env` for local work. |
| `.gitignore` | Files git ignores: secrets, caches, build output, eval results, `v2_docs/`, `docs_pdfs/`. |
| `.dockerignore` | What stays out of the Docker build context (secrets, caches, `.git`, `docs/`). |
| `.gcloudignore` | The same list, for what `gcloud` uploads to Cloud Build. |

## `.github/workflows/` — CI/CD

| File | Function |
| --- | --- |
| `ci.yml` | On every push and PR: backend lint and unit tests, integration tests against a Postgres service container, and frontend typecheck, lint, tests, build, a check that generated types are current, and the Playwright smoke test. |
| `evals.yml` | Runs the evals against the real model with the separate eval key when agents, prompts, the model layer, evals, catalogue or fixtures change. One run at a time. |
| `deploy.yml` | Manual deploy: builds the image in Cloud Build and rolls Cloud Run over to it, authenticated by Workload Identity Federation (no keys). |

## `deploy/`

| File | Function |
| --- | --- |
| `create-secrets.sh` | Creates the seven Secret Manager secrets and grants the runtime service account read access to each. Values the user supplies are asked for with hidden input, `IP_HASH_SECRET`, `VISITOR_HASH_SECRET` and `OWNER_TOKEN` are generated, and a secret that already exists is kept; `ROTATE=1` asks for all values again. |

## `db/` — database

| File | Function |
| --- | --- |
| `migrations/001_init.sql` | Initial schema: `runs`, `run_steps`, `price_catalogue`, `dish_cache`, `usage_counters`. |
| `migrations/002_dish_cache_prompt_version.sql` | Ties dish cache rows to the ingredient prompt version, so a prompt change misses old rows. |
| `migrations/003_record_answering_model.sql` | Records which model answered each step and whether the fallback was used. |
| `migrations/004_run_records.sql` | Run and step record columns written by the orchestrator. |
| `migrations/005_request_budget.sql` | `usage_counters` counts requests per model per Pacific day, with visitor and hashed-IP rows. |
| `migrations/006_would_cost.sql` | Adds `would_cost_usd`: what a run would cost at paid-tier rates. |
| `migrations/007_regular_price.sql` | Adds `regular_price`, the shelf price after a special ends. |
| `migrations/008_visitors.sql` | The `visitors` usage counter (keyed hash of the cookie id, run count, owner flag; no run id, no IP) and `runs.is_owner`. |
| `seed/price_catalogue.json` | The seeded, fictional Coles and Woolworths catalogue (168 ingredients, 602 products), with dates relative to an anchor day. |
| `seed/EDGE_CASES.md` | The deliberate edge cases built into the seed (store-only items, pack gaps, expiring specials…), each checked by a test. |

## `docs/` — documentation

| File | Function |
| --- | --- |
| `README.md` | Index of the docs: what each one is and when to read it. |
| `Multi-Agent Grocery Planner Design Document.md` | The whole design in one place: agents, decision rule, guardrails, evals, API, limitations, final architecture. |
| `DECISIONS.md` | Step-by-step log of every open choice and everything that broke, with numbers. |
| `NUMBERS.md` | Source, query and date for every figure quoted in the README. |
| `DEPLOY.md` | Cloud Run runbook: commands, rollback, secret rotation, costs, smoke test. |
| `DEVELOPMENT.md` | Local setup, running and testing the API, CLI, database, evals and tracing. |
| `REPO_MAP.md` | This file. |
| `screenshots/*.png` | README images (hero, split result, trace panel, unavailable screen), made by `frontend/scripts/`. |

## `backend/` — Python service

| File | Function |
| --- | --- |
| `pyproject.toml` | Package metadata, dependencies, ruff and pytest settings (markers `integration`, `live`). |
| `uv.lock` | Locked dependency versions for `uv sync --frozen`. |
| `.python-version` | Python version used by uv. |
| `examples/request_1.json`, `request_2.json` | Sample plan requests for the CLI. |

### `backend/grocery_planner/` — entry points and shared setup

| File | Function |
| --- | --- |
| `main.py` | ASGI entry point for uvicorn. Builds the app lazily from the environment (`API_STORE=memory` for no database). |
| `cli.py` | Runs a full plan from a JSON request file and prints the result (Postgres or `--memory`). |
| `config.py` | Active stores (Coles, Woolworths) and guardrail limits (daily request limit and per-visitor caps). |
| `env.py` | Reads environment variables, falling back to `backend/.env`. |

### `agents/` — the three LLM agents

| File | Function |
| --- | --- |
| `ingredient.py` | Ingredient agent: the model expands dishes into per-serving ingredients (one batched call, cached per dish). Code scales, merges, rounds and guards against avoids. |
| `price.py` | Price agent: code pre-fetches candidates per store, one batched model call picks a product per item, and code enforces match tiers, pack counts and costs. |
| `planner.py` | Planner agent: code computes coverage, the rule verdict, savings, flags and confidence. The model interprets them, can test splits with a `what_if_split` tool and writes the reasoning. |

### `orchestrator/` — runs the pipeline

| File | Function |
| --- | --- |
| `runner.py` | Fixed sequence ingredient → price → stop check → planner, with at most one loop-back (reprice, planner again). Writes every step as it happens. |
| `store.py` | Where runs and steps are saved: in memory for tests, Postgres in production. Each write refreshes the heartbeat the stale sweep reads. |
| `events.py` | Rebuilds the SSE event stream from stored state, so a client can reconnect and replay. |
| `models.py` | Run and step record types (statuses, step names, attempts). |
| `budget.py` | Reserves the worst-case request count (9) before a run starts. Raises when the day is used up or the service is busy. |

### `api/` — HTTP layer

| File | Function |
| --- | --- |
| `app.py` | `create_app(settings, services)`: the FastAPI app (create a run, read it, stream it, availability), the owner cookie and usage stats endpoints, plus static frontend serving. |
| `queue.py` | In-process run queue: fixed worker threads and a bounded FIFO, so runs don't depend on a connected client. |
| `schemas.py` | Public request and response models: the OpenAPI contract the frontend types are generated from. |
| `views.py` | Builds the public view of a run from safe fields only (no visitor or IP data, Langfuse IDs, raw errors). |
| `services.py` | Wires the real services from the environment: store, Gemini client, budget, tracer. Shared with the CLI. |
| `settings.py` | API settings from the environment. Refuses to start on bad config and locates the built frontend. |
| `daily.py` | `POST /internal/daily`, called by Cloud Scheduler with a verified Google OIDC token: re-seeds prices, sweeps stale runs, prunes. |
| `errors.py` | Turns errors into `{code, message}` responses, never stack traces. |
| `openapi.py` | Prints the OpenAPI schema used to generate the frontend types. |

### `llm/` — swappable model layer

| File | Function |
| --- | --- |
| `types.py` | Provider-neutral interface (`LLMClient`), results and errors. No provider SDK imports. |
| `gemini.py` | Gemini client on google-genai. Runs its own tool loop, retries, schema repair and fallback model, so every request is visible and counted. |
| `fake.py` | Scripted client with the same interface, for unit tests. |
| `ratelimit.py` | Per-model requests-per-minute limiter (in process). |
| `timeouts.py` | Per-agent timeout for a single model request. |
| `probe.py` | Sends one tiny structured request to check a model works. |

### `tools/` — deterministic tools

| File | Function |
| --- | --- |
| `price_lookup.py` | Price source interface with seeded-file and Postgres versions. Handles catalogue search and ranking, seed date resolution and sale weeks. |
| `planner_math.py` | Pure Decimal maths for the planner: basket totals, splits, savings, any number of stores. |
| `vocabulary.py` | Canonical generic ingredient list, derived from the catalogue. |
| `avoid_rules.py` | Avoid categories and the vocabulary items each forbids. The code guard behind the ingredient prompt. |
| `seed_sql.py` | Outputs SQL inserts for the catalogue from the seed JSON. |
| `bench_search.py` | Times catalogue searches against the real database. |

### `domain/` — shared types

| File | Function |
| --- | --- |
| `request.py` | The plan request (meals, people, days, avoid, pantry toggle) with validation limits. |
| `catalogue.py` | Store enum, catalogue product, pack units, brand labels, quality bands. |
| `ingredients.py` | Ingredient types the model returns and the consolidated list. |
| `priced_item.py` | A priced item per store: match tier, confidence, product fields. |
| `config.py` | User-editable planner parameters (savings thresholds, stop threshold). Out-of-range values are rejected. |
| `dish_cache.py` | Normalised dish and avoid cache keys. |

### `db/`, `guardrails/`, `tracing/`

| File | Function |
| --- | --- |
| `db/__init__.py` | Database URL and connection settings (works through Supabase's pooler). |
| `db/migrate.py` | Applies pending migrations in order, all in one transaction. |
| `db/seed.py` | Upserts the seed catalogue into Postgres for a given anchor date (run daily). |
| `db/dish_cache.py` | Dish cache repository (memory and Postgres). |
| `db/visitors.py` | The usage counter: the visitor hash, a repository that records admitted runs, and the stats (memory and Postgres). |
| `guardrails/budget.py` | Daily request budget and per-visitor limits, stored in `usage_counters` (admission limit, reservations, day stop). |
| `guardrails/clock.py` | Pacific-day and Sydney time helpers. Quota resets at midnight Pacific. |
| `tracing/base.py` | Tracer interface, plus no-op and safe wrappers so tracing can never break a run. |
| `tracing/langfuse_tracer.py` | Langfuse tracer: one trace per run, with spans per step and generations per model request. |
| `tracing/recording.py` | Tracer that records the tree it receives, for tests. |
| `tracing/pricing.py` | What a request would cost at paid-tier rates (the free tier costs $0). |
| `*/__init__.py` | Package exports (e.g. `tracer_from_env` in `tracing/`). |

### `backend/evals/` — evaluation harness

| File | Function |
| --- | --- |
| `run.py` | Entry point (`python -m evals.run`). Needs the eval key, writes results to `evals/results/` and exits non-zero on a regression. |
| `cases.py` | Loads the YAML case files. |
| `cases/ingredient.yaml`, `price.yaml`, `planner.yaml`, `e2e.yaml` | Eval cases per agent, plus full pipeline runs. |
| `execute.py` | Runs one case against the real agents. |
| `checks.py` | Deterministic checks. Each case reports pass rates per check. |
| `avoid.py` | Word lists that decide whether an ingredient breaks an "avoid". |
| `builders.py` | Builds planner inputs from saved price fixtures. No API calls. |
| `harness.py` | Eval model client (separate key, pinned model, no fallback) and the usage meter. |
| `report.py` | Scoring, the baseline gate and the markdown summary. |
| `baseline.json` | The approved baseline scores that runs are gated against. |
| `results/.gitkeep` | Keeps the (ignored) results folder in git. |

### `backend/tests/` — pytest

| File | Function |
| --- | --- |
| `conftest.py`, `helpers.py` | Shared fixtures and builders for priced items from the real seed. |
| `fixtures/price_*.json`, `fixtures/regenerate.py` | Saved price agent outputs, reused by evals, plus a script to re-anchor them to a new date. |
| `test_domain.py`, `test_request_and_vocab.py`, `test_dish_cache.py` | Domain types, request validation, vocabulary, cache keys. |
| `test_price_lookup.py`, `test_seed_edge_cases.py`, `test_planner_math.py` | Tools: search, every seeded edge case, planner maths. |
| `test_seed_expansion.py` | The second batch of 65 ingredients: both stores, ids, specials, original rows unchanged, name collisions. |
| `test_ingredient_agent.py`, `test_price_agent.py`, `test_planner_agent.py` | Agents with scripted models. |
| `test_orchestrator.py` | The pipeline, loop-back and stop paths. |
| `test_llm.py`, `test_timeouts.py` | Model layer: retries, repair, fallback, timeouts. |
| `test_guardrails.py` | Budget, reservations, day stop, visitor limits. |
| `test_tracing.py` | Trace shape and the guarantee that tracing never breaks a run. |
| `test_api.py`, `test_daily.py` | API through TestClient, and the daily job's auth and work. |
| `test_visitors.py` | The usage counter: counting on admit, refusals, the owner, the stats endpoint's auth, no hash in any response. |
| `test_evals.py` | The eval harness offline: cases, checks, gate, report. |
| `test_integration_db.py`, `test_api_integration.py`, `test_visitors_db.py` | `integration` marker: real Postgres. |
| `test_*_live.py` (`llm`, `ingredient`, `price`, `planner`) | `live` marker: real Gemini calls, run by hand. |

## `frontend/` — React app

| File | Function |
| --- | --- |
| `README.md` | Frontend commands (dev, build, test, e2e, gen:types). |
| `package.json`, `package-lock.json` | Dependencies and npm scripts. |
| `index.html`, `public/favicon.svg` | HTML shell and icon. |
| `vite.config.ts` | Vite + React + Tailwind. The dev server proxies `/api` to the backend. Vitest config. |
| `tsconfig.json`, `eslint.config.js` | TypeScript (strict) and lint settings. |
| `playwright.config.ts`, `e2e/smoke.spec.ts` | Playwright smoke test with every `/api` call mocked. |
| `scripts/gen-types.mjs` | Regenerates `src/api/types.ts` from the OpenAPI schema. |
| `scripts/screenshots.mjs`, `scripts/hero-shot.mjs` | Save README screenshots from the running app. |

### `frontend/src/`

| File | Function |
| --- | --- |
| `main.tsx`, `App.tsx` | Mounts the app and defines routes. |
| `index.css` | Tailwind import and theme (one accent colour). |
| `api/types.ts` | **Generated** from OpenAPI. Do not edit. |
| `api/client.ts` | Typed fetch calls to the API. |
| `api/events.ts` | SSE subscription with replay from the last seen event. |
| `pages/FormPage.tsx` | The meal form: presets, validation, submit and refusal messages. |
| `pages/RunPage.tsx` | Loads a run by ID and shows progress, then the result. |
| `pages/NotFoundPage.tsx` | 404 page. |
| `components/Layout.tsx`, `ui.tsx` | Page frame and small shared UI primitives (buttons, cards…). |
| `components/MealCard.tsx`, `AdvancedSettings.tsx` | One meal's inputs, and the planner thresholds. |
| `components/ProgressView.tsx` | Live per-agent progress. |
| `components/TracePanel.tsx` | Public trace panel built from the step records. |
| `components/UnavailableScreen.tsx` | Shown when the day's quota is used up (with reset time) or the provider refuses. |
| `components/result/ResultView.tsx` | Finished run: recommendation, then evidence, then list. |
| `components/result/RecommendationCard.tsx` | Verdict, savings, confidence. |
| `components/result/ComparisonTable.tsx` | Per-item, per-store picks with tiers and flags. |
| `components/result/Notes.tsx` | Planner flags explained. |
| `components/result/ShoppingList.tsx` | Shopping list grouped by category. |
| `components/result/StoppedView.tsx` | Stopped or failed run: why, and what was kept. |
| `components/result/RunFooter.tsx` | Share link (copy) and run details. |
| `lib/validation.ts` | Client-side validation that mirrors the API's limits. |
| `lib/progress.ts` | Turns events into per-step progress state. |
| `lib/format.ts` | Date and money formatting. |
| `lib/presets.ts` | Example meal plans for the form. |
| `test-setup.ts`, `test-fixtures.ts`, `**/*.test.ts(x)` | Vitest setup, sample runs, and unit and component tests. |

## Not in git

| Folder | What it is |
| --- | --- |
| `v2_docs/` | Work in progress, not part of the release (ignored). |
| `docs_pdfs/` | Local PDF copies of `docs/` (ignored). |
| `backend/.env` | Local secrets (ignored). |
