# Decisions log

Choices made in step 1 that the design doc did not specify. Newest decisions go at the bottom.

## Tooling
- **uv installed via a throwaway venv.** The machine had no uv and Python 3.10; uv fetched Python 3.12 itself. `.python-version` pins 3.12 and `requires-python` is `>=3.12,<3.13`.
- **Versions** were taken from what `uv add` resolved on 2026-10-13 and are locked in `backend/uv.lock`; `pyproject.toml` uses `>=` lower bounds of those versions.
- **`httpx2`, not `httpx`,** is the dev dependency for FastAPI's `TestClient`: Starlette 1.8 emits a deprecation warning when `httpx` is used.
- **hatchling** is the build backend; `pytest` also sets `pythonpath = ["."]` because the editable-install `.pth` was not picked up in this environment.
- **Ruff** rules: E, F, I, UP, B; line length 100.
- `.claude/` is git-ignored (local tooling config).

## Domain
- **Store enum** is `coles | woolworths`. "Aldi by data only" is interpreted as: one enum line plus seed rows, no schema change (the DB column has no CHECK on store).
- **`price` is the current shelf price.** When `on_special` is true it is the special price; no separate "was" price is stored.
- **`unit_price`** is a computed field rounded to 4 dp (half up): per 100 for `g`/`ml`, per item for `each`.
- **Floats are rejected** for `price` and `pack_size` (validator), so seed JSON stores them as strings.
- `special_end_date` is only allowed when `on_special` is true.
- `data_source` is `Literal["seeded"]` as asked; widening it is a one-line change later.
- `brand_label` allows only `house_brand | branded`. "On special" is a separate flag, as the doc labels it separately.
- **Quality bands:** house brand and branded = `standard`; premium house lines ("Coles Finest", "Woolworths Gold") = `premium`; a few staples (rice, pasta, canned tomatoes, baked beans) have a `budget` house brand so tier-difference flags can be tested.

## Schema (db/migrations/001_init.sql)
- `runs.id` is `uuid` default `gen_random_uuid()` (v4, 122 random bits).
- Extra columns, each commented as inferred in the SQL: `runs.created_at/finished_at/stop_reason`, split `tokens_in/tokens_out`, `run_steps.seq/attempt/error`, `dish_cache.dish_key/model/created_at`, `usage_counters` layout.
- `run_status` values: queued, running, completed, stopped, failed. "stopped" = orchestrator thresholds, "failed" = errors or budget exhaustion.
- `usage_counters` is one row per `(day, scope, scope_key)` with scope `global | visitor | ip`; the day-stop flag lives on the global row; `day` is the Pacific-time date.
- `price_catalogue.unit_price` is a **generated column**, so it cannot disagree with price and pack size.
- Primary key of `price_catalogue` is `(store, product_id)`; product IDs are `col-`/`wol-` + slug + `-01` house brand, `-02` branded, `-03` premium.
- RLS enabled on all five tables with no policies. The migration was parsed with `pglast` but **not executed** against a real Postgres.
- `dish_cache` key is the normalised dish name only. Open question for the ingredient-agent step: the avoid field is per meal, so substitution has to happen after the cache lookup, not inside the cached value.

## Seed
- **103 ingredients, 346 products**, every ingredient has 1-3 candidates per store. Reference date is 2026-10-12 (Monday); all timestamps are that date.
- **Generated once by a throwaway script** (not committed); the JSON is the source of truth and is edited directly from now on.
- Loader is a module, `grocery_planner.tools.seed_sql`, not a loose script, so it reuses the model and can be tested. Output is idempotent (`ON CONFLICT ... DO UPDATE`) in one transaction.
- The generated SQL file is git-ignored; the JSON is the committed source.

## Price lookup
- **Matching:** the lowercased, trimmed query must appear as a whole-word phrase in the product name or any search term. Whole-word so "egg" does not hit "eggplant". Blank query matches nothing.
- **Ordering:** results are sorted by `unit_price` then `product_id` before `limit` is applied, so truncation is deterministic and keeps the cheapest. `limit <= 0` returns `[]`.
- **`PostgresPriceSource`** fetches the store's rows and reuses the same Python matcher, so the two sources cannot drift apart. It opens one connection per call; there is no pool at demo scale.
- **`packs_needed`** accepts `Decimal` or `int`, raises `UnitMismatchError` (a `ValueError`) on unit mismatch and `ValueError` for non-positive quantities. No kg-to-g conversion (the doc says there is no conversion tool).
- **`pack_size_within_tolerance`** measures the gap relative to the *needed* size, inclusive at exactly the tolerance. Default tolerance is `Decimal("0.15")`.
