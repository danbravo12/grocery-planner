# Grocery Planner

A multi-agent grocery planner: meals in, shopping list, supermarket prices, and a where-to-shop recommendation out. Full design: [docs/Multi-Agent Grocery Planner Design Document.md](docs/Multi-Agent%20Grocery%20Planner%20Design%20Document.md). Choices the design doc left open are logged in [docs/DECISIONS.md](docs/DECISIONS.md).

**Status: step 3 of the build plan.** Scaffold, database schema and migrations, seeded price catalogue, price lookup (file and Postgres), and the planner's deterministic maths. There are no agents, LLM calls, orchestrator, API endpoints (beyond `GET /health`) or frontend yet.

## Layout

```
backend/    Python 3.12 package `grocery_planner` (FastAPI, Pydantic v2, psycopg 3)
frontend/   placeholder (React + Vite later)
db/migrations/   plain-SQL schema
db/seed/         seeded price catalogue + EDGE_CASES.md
docs/       design doc and DECISIONS.md
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

## Known limitation: Supabase free tier pauses

Free Supabase projects can pause after inactivity. A keep-alive ping against `/health` (plus a cheap DB query) is planned; check Supabase's current free-tier terms before launch.

## Tracing (Langfuse)

Set `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` and `LANGFUSE_HOST` (the host of your project's region) in `backend/.env`; without the keys tracing is simply off, and `LANGFUSE_ENABLED=false` switches it off even with them. `APP_ENV` (`dev`, `eval` or `prod`) tags every trace.

Each run is one trace named `plan_run` (its id is derived from the run id and stored in `runs.langfuse_trace_id`): a span per step, a generation per model request, and the planner's what-if calls under the planner span. That is about 7 observations for a normal run, so the free plan (50,000 units a month) covers several thousand runs. The cost Langfuse shows is what the run would cost on Google's paid tier; the free tier actually charges nothing. Langfuse keeps 30 days of history, while `run_steps` stays in your database. The CLI prints the trace id and URL after a run.

A tracer error never affects a run. Tests use a recording tracer and an in-memory exporter, so no network is needed.

## Known limitations

- Per-visitor limits (5 runs per visitor and 20 per IP per Pacific day) rely on a cookie and an IP, so they can be bypassed by clearing the cookie or changing IP. There are no accounts.
- The per-minute request limiter lives in one process; running several backend processes would multiply the limit.
