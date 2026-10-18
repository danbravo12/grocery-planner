# Development guide

How to run, test and extend the project locally. The front page is the [README](../README.md); how it is deployed is in [DEPLOY.md](DEPLOY.md). Paths are relative to the repository root.

## Layout

```
backend/    Python 3.12 package `grocery_planner` (FastAPI, Pydantic v2, psycopg 3)
frontend/   React + Vite + TypeScript + Tailwind (served by the API in production)
deploy/     helper for the Cloud Run secrets (see docs/DEPLOY.md)
Dockerfile  the image Cloud Run runs (frontend build + API, one worker)
db/migrations/   plain-SQL schema
db/seed/         seeded price catalogue + EDGE_CASES.md
docs/       design doc, DECISIONS.md, DEPLOY.md, NUMBERS.md (this guide too)
```

## Setup and running tests

On a fresh Mac, install [uv](https://docs.astral.sh/uv/) with Homebrew; uv then fetches Python 3.12 itself. No network or database is needed to run the tests.

```bash
brew install uv
cd backend
uv sync
uv run pytest
uv run ruff check .
```

Tests marked `integration` need a real Postgres and are skipped when `SUPABASE_DB_URL` is not set. To run everything, copy `.env.example` to `backend/.env` (git-ignored) and set `SUPABASE_DB_URL`. To run only the offline tests:

```bash
uv run pytest -m "not integration"
```

## Seeded prices are fictional

Every price in `db/seed/price_catalogue.json` is made up for testing, in AUD, and plausible rather than real. Brand names other than Coles and Woolworths (Kingsway, Harvest Farm, Bellvue, Goldleaf) are invented. Nothing here comes from a supermarket. Every product carries `data_source: "seeded"`. See `db/seed/EDGE_CASES.md` for the deliberate test cases.

## Model layer and live tests

`grocery_planner/llm/` holds a provider-neutral `LLMClient`, a Gemini implementation (`GeminiClient`) and a scripted `FakeLLMClient` for tests. Set `GEMINI_API_KEY` (and optionally `GEMINI_MODEL`, default `gemini-3.1-flash-lite`) in `backend/.env`. If the primary model is unavailable the call is retried once on `GEMINI_FALLBACK_MODEL` (default `gemini-3.5-flash-lite`); set `GEMINI_FALLBACK_MODEL=none` (or build the client with `fallback_model=None`) to pin a run to one model, as evals should, and check `result.fell_back` / `result.model`. Normal test runs never call Gemini. The live smoke test uses a few free-tier requests:

```bash
uv run pytest -m live -s
```

## Running a plan from the command line

```bash
cd backend
uv run python -m grocery_planner.cli run examples/request_1.json            # stored in Postgres
uv run python -m grocery_planner.cli run examples/request_2.json --memory   # no database
```
The CLI refuses to start (exit code 3) when the day's AI budget is used up, and prints the per-model counters after every run. Options: `--as-of YYYY-MM-DD` (default today, Sydney), `--bypass-cache` (skip the dish cache), `--seed` (refresh the Postgres catalogue first), `--memory` (in-process store and the seed file). Exit code 0 completed, 1 stopped (too little to compare), 2 failed, 3 unavailable today. A request file is a plan request plus optional `parameters` (savings and stop thresholds).

## Run the API locally

```bash
cd backend
uv run uvicorn grocery_planner.main:app --port 8000          # Postgres, real Gemini (backend/.env)
API_STORE=memory uv run uvicorn grocery_planner.main:app     # no database, seed file prices
```

Interactive docs are at `/docs` when `APP_ENV=dev` (hidden otherwise). A plan, end to end:

```bash
curl -si -c jar -X POST localhost:8000/api/runs -H 'content-type: application/json' \
     --data @examples/request_1.json            # 202 {run_id, run_url, events_url}
curl -N localhost:8000/api/runs/<run_id>/events # server-sent events until run_completed
curl localhost:8000/api/runs/<run_id>           # the finished run
```

| Endpoint | Purpose | Status codes |
| --- | --- | --- |
| `GET /health` | liveness | 200 |
| `GET /api/availability` | open today? reset time, sample run id | 200 |
| `POST /api/runs` | start a plan (body: the meal form plus optional `parameters`) | 202; 400 bad JSON; 413 body over 32 KB; 422 field messages; 429 `visitor_limit` / `ip_limit`; 503 `unavailable` / `day_budget` / `busy` |
| `GET /api/runs/{id}` | public run view | 200; 404 |
| `GET /api/runs/{id}/steps` | step records for the "how it worked" panel | 200; 404 |
| `GET /api/runs/{id}/events` | SSE: replays from `Last-Event-ID` (or `?after=`), then live; heartbeat comment every 15 s; closes after `run_completed` or `run_failed` (a stopped run too); `queued` while waiting | 200; 404 |

Errors are `{code, message}`. Settings (see `.env.example`): `FRONTEND_ORIGINS` (CORS), `TRUSTED_PROXY`, `IP_HASH_SECRET` and `SAMPLE_RUN_ID` (both required with `APP_ENV=prod`, or the API will not start), `API_STORE=memory`. At most 2 plans run at once with up to 10 waiting; a 13th request gets 503 `busy`. The queue is in one process, like the per-minute limiter.

## Run the frontend

The app is `frontend/` (React, Vite, TypeScript, Tailwind). In production FastAPI serves the built app itself, so the page and the API share one origin: no CORS, and the visitor cookie just works. Unknown paths that are not `/api` return `index.html`, so a reload of `/runs/<id>` works.

```bash
# one terminal: the API
cd backend && uv run uvicorn grocery_planner.main:app --port 8000
# another: the dev server with hot reload (proxies /api to :8000)
cd frontend && npm ci && npm run dev          # http://localhost:5173

# or build once and let FastAPI serve it
cd frontend && npm run build                  # then open http://localhost:8000
```

Checks: `npm run typecheck`, `npm run lint`, `npm test` (Vitest and Testing Library), `npm run e2e` (a Playwright smoke test with every `/api` route mocked). The API types in `frontend/src/api/types.ts` are generated from the OpenAPI schema (`npm run gen:types`); CI regenerates them and fails if the committed file differs.

**Try an example.** The form has four ready-made plans, each checked against the seeded prices to give a different answer: a close call (Friday dinner), one store clearly wins (Sunday lamb), worth splitting the shop (lamb and pork weekend) and an answer that depends on a special ending soon (big chicken BBQ). They work because the seed has two deliberate price gaps (lamb chops are much cheaper at Coles, pork chops much cheaper at Woolworths) on top of the existing Woolworths chicken special.

**How it worked.** Every run page has a collapsed "How it worked" panel (it reads `/api/runs/{id}/steps`, so it works on shared links): one row per step with the model, requests, tokens and latency, expandable to cache hits, price downgrades by reason, tool calls (what-if split arguments and results) and how many of the run's reserved requests were used. No prompts are shown.

**Sample run.** `SAMPLE_RUN_ID` (the run shown when the day's free capacity is used up) should be a completed run that does not go stale. The split-the-shop run `f95caaf0-a6fc-4587-9395-7422e48bf67d` in the project's Supabase database is the one chosen; set it as `SAMPLE_RUN_ID` in production.


## Database: migrate, seed, integration tests

With `SUPABASE_DB_URL` set in `backend/.env`:

```bash
cd backend
uv run python -m grocery_planner.db.migrate          # applies db/migrations/*.sql once each, in one transaction
uv run python -m grocery_planner.db.seed             # upserts the catalogue; optional arg: anchor date YYYY-MM-DD
uv run pytest -m integration                         # price source equivalence, idempotent migrations, RLS check
```

Seed dates are relative ("special ends in 3 days"), so re-run the seed daily to keep specials current; it resolves against today in Australia/Sydney unless you pass an anchor date. The integration tests also run the seed with today's anchor.

To get plain SQL instead: `uv run python -m grocery_planner.tools.seed_sql [anchor] > ../db/seed/price_catalogue.sql`.

## Running the evals

```bash
cd backend
uv run python -m evals.run                       # everything, default model
uv run python -m evals.run --agent planner       # one agent (repeatable)
uv run python -m evals.run --model gemini-3.5-flash-lite --repeat 3
uv run python -m evals.run --list
```

It writes `evals/results/<timestamp>-<model>.json` and a markdown summary, and exits 1 if an agent is more than 10 points below `evals/baseline.json`, below its floor (ingredient 85%, price 85%, planner 90%), or if any answer came from a fallback model. A full run takes about 5 minutes at the 12 requests a minute limit, so it is not part of the normal test run.

Request counting: a case's call limit counts model calls (first calls and tool rounds); repairs and transport retries (5xx, timeouts, per-minute 429s) are reported but never fail a case, and a per-day 429 stops the run with exit code 3 instead of producing failures. The three end-to-end cases all expect "no meaningful difference" because the seeded prices are close; the planner cases cover the other outcomes (one store, split, and the two overrides).

## Tracing (Langfuse) setup

Set `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` and `LANGFUSE_HOST` (the host of your project's region) in `backend/.env`; without the keys tracing is simply off, and `LANGFUSE_ENABLED=false` switches it off even with them. `APP_ENV` (`dev`, `eval` or `prod`) tags every trace.

Each run is one trace named `plan_run` (its id is derived from the run id and stored in `runs.langfuse_trace_id`): a span per step, a generation per model request, and the planner's what-if calls under the planner span. That is about 7 observations for a normal run, so the free plan (50,000 units a month) covers several thousand runs. The cost Langfuse shows is what the run would cost on Google's paid tier; the free tier actually charges nothing. Langfuse keeps 30 days of history, while `run_steps` stays in your database. The CLI prints the trace id and URL after a run.

A tracer error never affects a run. Tests use a recording tracer and an in-memory exporter, so no network is needed.
