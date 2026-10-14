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
- ~~`dish_cache` keyed on dish name only~~ superseded in step 1b (now keyed on dish and avoid).

## Seed
- **103 ingredients, 346 products**, every ingredient has 1-3 candidates per store. Dates are relative to an anchor (see step 1b); tests use 2026-10-12 (Monday).
- **Generated once by a throwaway script** (not committed); the JSON is the source of truth and is edited directly from now on.
- Loader is a module, `grocery_planner.tools.seed_sql`, not a loose script, so it reuses the model and can be tested. Output is idempotent (`ON CONFLICT ... DO UPDATE`) in one transaction.
- The generated SQL file is git-ignored; the JSON is the committed source.

## Price lookup
- **Matching:** the lowercased, trimmed query must appear as a whole-word phrase in the product name or any search term. Whole-word so "egg" does not hit "eggplant". Blank query matches nothing.
- **Ordering:** results are sorted by `unit_price` then `product_id` before `limit` is applied, so truncation is deterministic and keeps the cheapest. `limit <= 0` returns `[]`.
- **`PostgresPriceSource`** fetches the store's rows and reuses the same Python matcher, so the two sources cannot drift apart. It opens one connection per call; there is no pool at demo scale.
- **`packs_needed`** accepts `Decimal` or `int`, raises `UnitMismatchError` (a `ValueError`) on unit mismatch and `ValueError` for non-positive quantities. No kg-to-g conversion (the doc says there is no conversion tool).
- **`pack_size_within_tolerance`** measures the gap relative to the *needed* size, inclusive at exactly the tolerance. Default tolerance is `Decimal("0.15")`.

## Step 1b
- **Relative seed dates.** The seed JSON now has `price_age_days` (days before the anchor) and `special_ends_in_days` (days after it) instead of absolute dates; `reference_date` is gone. `CatalogueProduct` still holds real dates. `resolve_seed_product` / `load_seed_products` in `price_lookup.py` do the conversion.
- **Anchor default** is today in `Australia/Sydney`, taken by `SeededFilePriceSource(anchor=...)` and `seed_sql(anchor=...)` (CLI: optional `YYYY-MM-DD` argument). `price_timestamp` resolves to midnight Sydney time on `anchor - price_age_days`. Added the `tzdata` dependency so this works in containers without a system tz database.
- **`special_active(product, as_of)`** is true when on special and `as_of` is on or before the end date (the end date itself counts as active). A special with no end date stays active. **`specials_ending_within(products, as_of, days)`** returns active specials whose end date is 0 to `days` days after `as_of`, inclusive.
- **Tests use a fixed anchor** (2026-10-12). EDGE_CASES.md describes case (c) as "ends 3 days after the anchor".
- **Brand filter.** `PriceSource.search(term, store, limit, brand=None)`. Brand is compared case-insensitively on the trimmed value, with exact equality (not substring), and applied before ranking and `limit` in both sources.
- **`dish_cache` key** is now `(dish_key, avoid_key)`, both normalised: lowercase, trimmed, whitespace collapsed. `avoid_key` is `''` for no avoid; otherwise the avoid text is split on commas, normalised, de-duplicated (my addition, so "nuts, nuts" matches "nuts"), sorted and rejoined with `,`. Helper: `make_dish_cache_key(dish_name, avoid)` in `domain/dish_cache.py`, returning `(dish_key, avoid_key)`. The SQL notes that rows are only written after the ingredient agent's output passes schema validation.
- **README** setup now uses `brew install uv`, `uv sync`, `uv run pytest`.
- **Known limitation:** `PostgresPriceSource` filters in Python after fetching all rows for a store. Acceptable at ~350 rows; a live source would search server-side.

## Step 2 (planner tools)
- **`Store.ALDI` added to the enum** (no seed rows). The N-store test needed a third real `Store`, and this is the one-line change the earlier decision anticipated. Seed and lookup tests that looped over `Store` now name Coles and Woolworths explicitly. Searching Aldi returns `[]`.
- **`PricedItem`** requires every product field (id, name, brand, size, price, unit price, packs, total cost, timestamp, source) for exact, equivalent and substitute rows, and none for `not_found`. `special_end_date` is the exception: it stays null for matched products that are not on special. Substitute rows keep their product and cost so they can be shown, but are never used in maths. No floats for money.
- **`PlannerConfig`** values are `Decimal` (so threshold comparisons are exact) with inclusive ranges; out-of-range raises `ValidationError`. JSON floats such as `0.08` are accepted and converted via their decimal text.
- **Item universe.** The items in a comparison are the distinct `item_id`s across the rows for the given stores. A store with no row for an item counts as missing it, the same as `not_found`. Rows for other stores are ignored. Two rows for the same item and store raise `ValueError`.
- **Coverage pct** is a `Decimal` fraction (0.9 = 90%). With zero items, `should_stop` stops with the reason "no items to compare".
- **Stop rule** compares strictly: `share > stop_not_found_pct` or a per-store matched-pct rule (replaced in step 2b by the comparability rule). Exactly at a threshold does not stop. All reasons are returned, not just the first.
- **Ties for cheapest store** go to the first store in the `stores` argument. In `best_split`, an item that costs the same at the cheapest single store and elsewhere stays with the cheapest single store, so a tie never creates an extra trip.
- **`what_if_split`** raises `ValueError` if an assigned item is not a common item (it is outside the basket being compared), not usable at its assigned store, or the store is not in `stores`. It may return negative savings if the assignment is worse than the best single store. The returned assignment covers every common item.
- **`per_item_savings`** `saving` = most expensive store's cost minus cheapest store's cost. `tier_gap_flag` is true if the picks differ in `brand_label` or `match_tier`. `special_flag` looks only at the cheapest pick and uses `specials_ending_within(..., as_of, 7)`, so a special ending exactly 7 days out counts and one that has already ended does not. `specials_ending_within` and `special_active` are now generic over anything with `on_special` and `special_end_date`.
- **`rule_verdict` order of checks:** split first (split savings >= threshold), then one_store (gap >= threshold), else no_meaningful_difference. This is equivalent to the doc's three conditions, which are mutually exclusive. Threshold is based on the cheapest single-store total on common items. Raises `ValueError` with fewer than two stores or no common items (the orchestrator is expected to have stopped the run first).
- **Verdict contents:** the `split` assignment lists every common item, including those that stay at the cheapest store. `store` is set only for `one_store`. `item_savings` is attached for the agent to explain from.
- **Money display:** `format_money` rounds half up to cents with thousands separators; nothing else rounds.
- **Test fixtures** (`tests/helpers.py`) build rows from the seed through `search` and `packs_needed` (one pack each). Large-gap and threshold scenarios are constructed by scaling or overriding the seed price (`factor` / `price`), because seed prices differ by only a few percent between stores.

## Step 2b
- **Comparability replaces the per-store stop rule.** `should_stop` no longer looks at any single store's matched share. It stops when `common items / total items < stop_comparable_pct`, where common = usable at every store. The not-found rule is unchanged. Both use strict comparisons, so exactly at a threshold does not stop. A store with low coverage no longer stops a run if enough items are comparable; the design doc's "any store has fewer than 50% matched" row is superseded by this.
- **Config field renamed** `stop_store_matched_pct` -> `stop_comparable_pct` (default 0.50, range 0.40 to 0.80, unchanged).
- **`Coverage` gained `common_items`** (ids usable at every store). Per-store `matched` and `pct` stay in the result for the UI but are not a stop rule.
- **Reason text:** `"no comparable items: only 0 of 10 items are usable at every store (0% < 50%)"`; when some but too few are comparable it starts `"too few comparable items: only 4 of 10 ..."`.
- **`rule_verdict` still raises on zero common items.** This is now an invariant: with `stop_comparable_pct` at least 0.40, `should_stop` stops any run with no common items before the verdict is reached. A comment says so.
- **`ACTIVE_STORES`** (`coles`, `woolworths`) lives in `grocery_planner/config.py`. Code must use it instead of iterating the `Store` enum, which can include stores without seed data (Aldi). Nothing in `grocery_planner/` iterated the enum; the tests that did now use `ACTIVE_STORES`. A test asserts every active store has seed products and that Aldi is neither active nor seeded, so adding Aldi to `ACTIVE_STORES` before it has seed rows fails the build.

## Step 3 (Supabase)
- **Check 0 (stop-rule denominators):** the code was right and a test name was wrong. Per-store `pct` and the comparability share both divide by all distinct item ids, and common items are a subset of every store's matched items, so a store below `stop_comparable_pct` always forces a stop. The old test "low single-store coverage alone does not stop" used a store at 60% (above 50%), so it never exercised a low store. It is replaced by a parametrised test over 0-10 missing items that asserts the implication (store below threshold means stop) and the shared denominator.
- **Migration runner** (`grocery_planner.db.migrate`): applies every pending file in one transaction (all or nothing), under an advisory lock so two runners cannot collide. Applied files are recorded in `schema_migrations` (filename, applied_at), which has RLS enabled with no policies like the other tables. Changing an already-applied file is not detected (no checksums); add a new migration instead.
- **Seed command** (`grocery_planner.db.seed`): one transaction, upsert on `(store, product_id)`. It never deletes, so a product removed from the JSON stays in the table until removed by hand.
- **Connections:** `SUPABASE_DB_URL` is read from the environment, falling back to `backend/.env` via python-dotenv (existing variables win). Connections use `prepare_threshold=None` so they also work through Supabase's transaction pooler. The URL is never printed. The configured URL here is the direct connection (port 5432).
- **`PostgresPriceSource`** uses a `psycopg_pool.ConnectionPool` (min 1, max 4) and has `close()`. Still fetches a store's rows and matches in Python (known limitation unchanged).
- **Integration tests** are marked `integration` and auto-skipped when no database URL is available (`tests/conftest.py`). They seed the real database with today's anchor (the same state the daily seed produces) and compare 24 terms x 5 brand filters x 2 limits x 2 stores against the file source. The RLS test checks every `public` table, so it also covers `schema_migrations`.
- **Latency** (direct connection from the dev machine, 50 calls, warm pool): median 27 ms, p95 32 ms, max 39 ms. About 60 searches per run is therefore under 2 s, so no per-run snapshot is proposed yet. Re-measure from the real host, since that number is dominated by network round trips.
- **Design doc synced** (see git diff): Gemini 2.5 to 3.5 Flash-Lite and the deprecation item ticked; one threshold for all verdicts, with the agent following the code's verdict unless it states an override reason; dish cache keyed on dish plus normalised avoid and written only after schema validation; comparability stop rule replaces the per-store rule (`stop_comparable_pct`); seeded specials relative to an anchor.
- The doc's decision rule now says "meet" (not "exceed") the threshold, matching the code.

## Step 4 (model layer)
- **SDK findings.** `google-genai` 2.30.1. Google's docs now lead with a newer Interactions API; this layer uses the long-standing `client.models.generate_content`, which is still in the SDK and has everything needed. Structured output: `response_mime_type="application/json"` plus `response_json_schema=Model.model_json_schema()`, and we validate with Pydantic ourselves. Function calling: `FunctionDeclaration(parameters_json_schema=...)`, `AutomaticFunctionCallingConfig(disable=True)`, calls read from `response.function_calls`, results sent back as `Part.from_function_response`. Usage: `usage_metadata.prompt_token_count`, `candidates_token_count`, and `thoughts_token_count` for thinking. Thinking: 3.5 Flash-Lite supports `thinking_level` (minimal, low, medium, high) and defaults to minimal; the lowest level is `minimal`. Docs say thinking tokens are billed with output tokens and count toward `max_output_tokens`.
- **Tools and a schema in one request** works on `gemini-3.1-flash-lite` (the live smoke test does exactly that). Docs do not state it explicitly; reports say it fails on 2.5 models.
- **Gemini 3 thought signatures:** during the tool loop the model's own turn is appended to the history unchanged before the tool results.
- **Sync, not async.** Agents run in a background task per run, one request at a time, so the interface is synchronous.
- **Interface.** `LLMClient` has `generate` and `generate_with_tools`; no google types leak out of `grocery_planner/llm/`. `GeminiClient.generate_with_tools` also takes an optional `temperature` (default 0.1), which the protocol omits as the spec's signature did. `LLMResult` is a frozen dataclass.
- **`attempts` equals `usage.requests`**: total requests sent for the call (first tries, retries, repairs, tool rounds). It is kept as a separate field because the spec asked for both.
- **Output tokens exclude thinking tokens** (`thinking_tokens` is separate), so `output + thinking` is the billed output.
- **Model id** from `GEMINI_MODEL`, default `gemini-3.5-flash-lite`. **Thinking level** defaults to `minimal`; the constructor takes any level, or `None` to leave the model default.
- **Retries.** "3 attempts total, backoff 1s, 2s, 4s" is inconsistent (3 attempts have 2 gaps), so I implemented 3 attempts with delays of 1s then 2s plus up to 25% jitter, and left 4s in the table for a fourth attempt. The design doc row was reworded to match. Each underlying request is retried independently, so a tool loop can send more than 3 requests in total. Retried: timeouts and other transport errors, any `ServerError` (5xx), and per-minute or unknown 429s. Not retried: other 4xx (raised as `LLMError`) and per-day 429s. A 429 retry does not honour Google's `retryDelay` hint (it can be 30s+); after 3 attempts it raises `QuotaExhausted` and the caller decides.
- **429 classification.** The error body is searched (lower-cased) for Google's quota ids and text: `PerDay` / "per day" / "daily" means `per_day` (checked first, since waiting seconds does not cure a daily limit), `PerMinute` / "per minute" means `per_minute`, anything else is `unknown`. The id names come from forum reports of real errors (e.g. `GenerateRequestsPerMinutePerProjectPerModel-FreeTier`), not from official docs, so the live behaviour should be re-checked the first time a real 429 is seen.
- **Schema repair** is one extra request that replays the conversation, shows the model its own bad reply, and lists the validation errors (`field: message`). It is sent without tools. An empty or blocked reply is treated as invalid output. Failure raises `SchemaValidationFailed(raw_outputs=[first, second], errors)`.
- **Tool loop.** A round is one model response that asks for tools (parallel calls in one response are one round). Unknown tool, invalid args and a `ValueError` raised by the tool are sent back to the model as `{"error": ...}`; other exceptions are bugs and propagate. When the model asks for tools more than `max_tool_rounds` times, `ToolLoopExceeded` is raised (that last request is still counted). `ToolSpec.func` is called with the validated args as keyword arguments.
- **Request accounting.** `on_request(delta)` is called once per request actually sent, including failed ones (a 429 or 5xx counts as a request; its delta has no tokens). The deltas sum to `result.usage`. If a call raises, the hook has still fired for everything sent. The budget itself is not built.
- **Timeout** is set per request (`HttpOptions(timeout=30000)` in the request config). The SDK's own retry option is not used.
- **`FakeLLMClient`** script entries: a model instance, an exception instance, or `FakeToolCalls(rounds, result)`, which runs the real tools through the same `execute_tool` helper. It counts one request per call plus one per tool round and fires `on_request`. It does not simulate retries or repair.
- **Live tests** (`-m live`) only run when `-m live` is passed (`tests/conftest.py`), and need `GEMINI_API_KEY`. `grocery_planner/env.py` now owns `.env` loading for both the database and Gemini settings.
- **Live finding: `gemini-3.5-flash-lite` returns 504 DEADLINE_EXCEEDED for every request with this key** (tried on 2026-10-14 with 30s and 60s timeouts, plain prompt, with and without a thinking level), while `gemini-3.5-flash` and `gemini-3.1-flash-lite` answer in 3 to 5s. So the smoke test was run on `gemini-3.1-flash-lite` via `GEMINI_MODEL`. The default stays `gemini-3.5-flash-lite` pending your decision. Also: a persistent 504 costs 3 x 30s plus backoff (about 90s) per request before `LLMUnavailable`, which the orchestrator should keep in mind.
- **Daily budget unverified.** The design doc now says 70% of 500 requests/day = 350 with a 15% buffer, as instructed; I could not confirm 500 RPD for 3.5 Flash-Lite from the public docs (the rate-limits page does not list per-model numbers), so the "read the limits" open item stays unticked.

## Step 4b
- **Default model is `gemini-3.1-flash-lite`** (still overridable with `GEMINI_MODEL`). The design doc's Stack, Model row and the "read the limits" open item now say 3.1 Flash-Lite, plus a Known limitations row about the 3.5 timeout.
- **Probe:** `python -m grocery_planner.llm.probe [--model X]` sends exactly one structured request and prints model, latency, request count and tokens, or the error class (with HTTP code and status) and exits 1. It uses a `GeminiClient(single_shot=True)`, a constructor flag that means one send, no retries and no schema repair (an invalid reply raises `SchemaValidationFailed` with the single raw output).
- **Timeout** is 20s per request (was 30s).
- **Attempts by failure type:** timeouts and transport errors, and any 504, get 2 attempts (one retry). Other 5xx, per-minute 429s and unknown 429s get 3 attempts. Backoff before a retry is 1s then 2s plus up to 25% jitter (the unused 4s step is gone). A persistent 504 now costs about 40s instead of 90s.
- **retryDelay on 429s.** The hint is read from `error.details[].retryDelay` (e.g. `"39s"`), falling back to "retry in 3.2s" in the message. For per-minute and unknown 429s: a hint up to 20s is waited out (plus 0 to 0.5s jitter, so the wait can be slightly over 20s) instead of the fixed backoff; a hint over 20s raises `QuotaExhausted(kind="per_minute")` at once with no retry, even if the 429 was otherwise unclassified. No hint means normal backoff. Per-day 429s are still never retried, hint or not. The hint is not checked against the day-stop logic; that stays with the guardrail layer.
- **Test impact:** attempt-count tests were split by failure type; new tests cover hint under cap, at the cap (20s is waited), over the cap, hint in the message, no hint, and single-shot. The exact-accounting scenario is unchanged (its failures are one 5xx and one hint-less per-minute 429).
- **Live result:** on 2026-10-14, after these changes, `gemini-3.5-flash-lite` answered the probe in about 1.1s, so the earlier 504s were transient (or lifted on Google's side) rather than a permanent problem with the project. The default stays 3.1 as instructed; switching back is a one-line change or an env var.
