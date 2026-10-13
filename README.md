# Grocery Planner

A multi-agent grocery planner: meals in, shopping list, supermarket prices, and a where-to-shop recommendation out. Full design: [docs/Multi-Agent Grocery Planner Design Document.md](docs/Multi-Agent%20Grocery%20Planner%20Design%20Document.md). Choices the design doc left open are logged in [docs/DECISIONS.md](docs/DECISIONS.md).

**Status: step 1 of the build plan.** Scaffold, database schema, seeded price catalogue and the price lookup tool. There are no agents, LLM calls, orchestrator, API endpoints (beyond `GET /health`) or frontend yet.

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

`PostgresPriceSource` tests are skipped unless `SUPABASE_DB_URL` is set (copy `.env.example` to `.env` and apply `db/migrations/001_init.sql` plus the seed SQL first).

## Seeded prices are fictional

Every price in `db/seed/price_catalogue.json` is made up for testing, in AUD, and plausible rather than real. Brand names other than Coles and Woolworths (Kingsway, Harvest Farm, Bellvue, Goldleaf) are invented. Nothing here comes from a supermarket. Every product carries `data_source: "seeded"`. See `db/seed/EDGE_CASES.md` for the deliberate test cases.

## Loading the catalogue into Postgres

Seed dates are relative ("special ends in 3 days"), so re-run the loader to refresh them; they resolve against today in Australia/Sydney unless you pass an anchor date.

```bash
cd backend
uv run python -m grocery_planner.tools.seed_sql > ../db/seed/price_catalogue.sql  # optional arg: anchor date YYYY-MM-DD
psql "$SUPABASE_DB_URL" -f ../db/migrations/001_init.sql -f ../db/seed/price_catalogue.sql
```

## Known limitation: Supabase free tier pauses

Free Supabase projects can pause after inactivity. A keep-alive ping against `/health` (plus a cheap DB query) is planned; check Supabase's current free-tier terms before launch.
