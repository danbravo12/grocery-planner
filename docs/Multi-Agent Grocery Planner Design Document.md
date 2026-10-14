# Multi-Agent Grocery Planner: Design Document

Oct 13, 2026 · @Daniel

## Overview and goals

This is a public web app where a user describes the meals they plan to cook, and three agents turn that into a shopping list, supermarket prices, and a recommendation on where to shop this week. The goal is a small but properly run production AI system that can be listed on a resume and discussed in interviews, not a product with scale.

**Goals**

- Show a multi-agent system with real judgment steps, tool use, and deterministic code where maths is needed
- Show production habits: structured outputs, cost control, tracing, evals in CI, failure handling, deployment
- Stay reliable on the free tier of the Gemini API, and say plainly when the service is unavailable for the day

**Non-goals**

- User accounts, payments, or personalisation
- Live scraping of supermarket sites in the MVP (prices are seeded, behind an interface built to swap)
- Route or distance optimisation (the planner decides on savings only; where the user shops is their call)
- Scale beyond a demo audience

## Architecture and run flow

A run moves through three agents in a fixed order, and only the planner can send work back, once.

&#91;embedded content: architecture · 3 agents, 1 loop-back\]

**Stack**

- **Backend:** Python and FastAPI, one background task per run, SSE for progress
- **Agents:** Gemini 3.1 Flash-Lite (free tier) behind a swappable model layer (a thin in-house layer over the google-genai SDK)
- **Data:** Supabase Postgres
- **Tracing:** Langfuse, plus step records in the app's own database for the public trace panel
- **Frontend:** React and Vite

## Input form

A form replaces free text so the agents start from structured input. The user adds one or more meals, and each meal has its own fields.

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| Meal name | text | yes | General name, e.g. "Friday dinner" |
| Mains | list of text | yes, at least 1 | e.g. fillet mignon, chicken nuggets |
| Minor sides | list of text | no | e.g. potato salad, fries |
| People | integer | yes | Range 1 to 12 |
| Days | integer | yes | Range 1 to 14 |
| Avoid | text | no | Per meal, e.g. "nuts" |

Two settings apply to the whole request:

- **Include pantry staples** (toggle, default off): salt, oil, pepper and similar are assumed to be at home and excluded unless the toggle is on.
- **Advanced settings** (collapsed panel): savings threshold, stop thresholds and pack-size tolerance, each with a default and a clamped range (see Configuration).

## Ingredient agent

This agent turns the meal form into one consolidated, quantity-scaled list of purchasable items, with its assumptions stated. It needs an LLM because the step is fuzzy: "potato salad" has no single recipe, and a script cannot decide which ingredients a dish probably needs.

**Input:** `meals[]` (each with meal name, mains, minor sides, people, days, avoid) and `include_pantry_staples`.

**Job**

1. Expand each dish into per-serving ingredients, in one batched model call per request for all dishes not already cached
2. Scale quantities by people × days (code)
3. Merge duplicates across meals into one list (code), by name and canonical unit, rounding countable items up to whole numbers
4. Name each ingredient from a catalogue-derived vocabulary of generic items, each with a canonical unit (an out-of-vocabulary name is allowed and flagged)
5. Respect each meal's avoid field by substituting, and say so in the assumptions

**Output per item:** name, quantity, unit, category (meat, produce and so on), which meals use it, a pantry-staple flag, an in-vocabulary flag, sanity flags for unusual amounts, and an optional brand that is filled only when the dish text explicitly names one. The output also carries a top-level `assumptions[]`.

**Rules**

- It maps to generic items, not store products. Matching to real products is the price agent's job, which keeps the two agents separate.
- Avoid is per meal, so the merged list reflects each meal's own constraints.
- It does not ask clarifying questions. A contradiction (for example avoid: dairy with a creamy potato salad) is resolved by substitution and noted.
- Names and units come from the vocabulary: the model must use a vocabulary name with its canonical unit when one fits, and code validates this (a mismatch triggers one schema repair). There is no unit-conversion tool.
- Dish expansion is cached per dish and normalised avoid text, written only after the output passes schema validation (see Data layer), and scaling, merging and rounding are code, not the model.

## Price agent

This agent finds a comparable product for each purchasable item at each store and works out what it costs to buy the amount needed. The MVP stores are Coles, Woolworths and optionally Aldi.

**Input:** the purchasable items list and the stores to check.

**Tool:** one price lookup that returns candidate products for a search term. At first it reads a seeded catalogue of about 100 ingredients. It returns the same fields a live source would, including a price timestamp, so swapping sources later is a config change. Seeded specials and price ages are stored relative to an anchor date (default: today), so specials never go stale.

**Flow:** code pre-fetches up to five candidates per item per store (only the named brand when the user named one) and computes packs, purchased quantity, leftover, total cost, unit price and whether a special is active. One batched model call then picks a candidate per item per store, or none, with a tier and confidence. Code enforces the tier rules afterwards, downgrading and flagging any pick that breaks them. If a store had no usable pick and the model suggested search terms, there is one retry round (a single extra model call). The planner's loop-back reuses the same machinery.

**Judgment calls**

- Choose the best match from the candidates
- Accept a pack count worked out by code (400g needed, 500g pack, so one pack)
- Retry with a different search term when nothing matches
- Suggest a substitute or mark the item not found
- Flag low-confidence matches

**Matching rules**

- **User names a brand:** match that exact brand at every store.
- **Generic item ("bacon"):** pick the best-value acceptable product at each store, branded or house brand, from products of the same type and a comparable quality band. A popular brand on special can beat the house brand.
- Each pick is labelled house brand, branded, or on special.
- The agent flags when a price gap comes from a tier difference rather than the store's pricing, so savings are not fake.
- **Pack-size tolerance:** pack size never changes a match tier, which depends only on the product (type, quality band, and brand for exact). Instead, code compares the quantities each store's pick makes you buy (packs × pack size): if they differ by more than 15% (relative to the smaller), the item gets a `pack_gap` flag carrying both quantities. Leftover is shown, not credited.

**Match tiers per item per store**

| Tier | Meaning | Used in comparison |
| --- | --- | --- |
| Exact | Same brand and product at both stores | Yes |
| Equivalent | Comparable product in the same quality band | Yes, with tier flagged |
| Substitute | Nothing comparable; closest alternative | Flagged, excluded from like-for-like totals |
| Not found | No usable product | Excluded and counted toward stop thresholds |

**Cost basis:** the cost of the packs actually bought, not a prorated amount. Leftover quantity is noted but not credited.

**Output per item per store:** product name, brand label, pack size, price, unit price, special (yes or no and end date), packs needed, total cost, match tier, match confidence, status, price timestamp, and `data_source` (seeded for now).

**Seeded edge cases for evals:** an item missing at one store, mismatched pack sizes, a special ending mid-week, and a near-identical basket total.

## Planner agent

This agent decides where it is worth shopping this week based on savings alone. It is not a route planner, and "no meaningful difference" is a complete, valid outcome: the user then shops wherever suits them. There is no home store field.

**Input:** each store's basket from the price agent (items, prices, match tiers, statuses).

**Deterministic tools** (code does the arithmetic, the agent decides which to call and interprets the results):

- Basket total per store, on common items only
- Per-item savings between stores
- What-if split totals, e.g. "meat from store B, everything else from store A"

**Decision rule:** one threshold applies to every outcome: $8 or 8% of the basket, whichever is higher, set in config. A split only wins if its savings meet the threshold after accounting for the extra trip. Otherwise one store wins if its lead over the next cheapest store meets the threshold, and anything less is no meaningful difference. Code computes this rule verdict, and the agent follows it unless it states an override reason. If one store is cheaper overall but another is cheaper on one expensive item, the agent only recommends a split when it clears the threshold, and otherwise mentions the item as a tip.

**Incomplete baskets:** if a store lacks a few items, the planner compares on common items and flags the gap.

**Output**

- `recommendation`: one\_store (which), split (which items where), or no\_meaningful\_difference
- `savings` and `reasoning` in plain language
- `confidence`, driven by match tiers and basket completeness
- `flags[]`: items missing at a store, savings from specials ending mid-week, substitute matches
- `assumptions[]`

**Loop-back:** the planner may send low-confidence items back to the price agent once. After that it must decide with what it has.

## Orchestrator

The orchestrator runs the three agents in a fixed sequence, with one controlled loop-back. It is predictable and cheap, and the loop-back keeps it agentic.

**Flow:** ingredient agent, then price agent, then planner. The planner may return low-confidence items to the price agent once.

**Failure handling:** when too much is missing for a comparison to mean anything, the run stops and tells the user. Otherwise it continues.

| Situation | Behaviour |
| --- | --- |
| A few items missing at one store | Planner runs on common items and flags the gap |
| More than 25% of items not found at any store | Stop and tell the user |
| Fewer than 50% of items comparable across all stores | Stop and tell the user |
| Transient error (timeout, rate limit) | Automatic retry with backoff before counting as a failure |
| Item not found | Never retried blindly; this is what the loop-back is for |

When a run stops, the user still sees the ingredient list that was built, plus which items failed and why, so they can shop from the list.

**Execution model:** each run is a background task that writes every step to the database. It does not depend on the browser connection, so a closed tab does not abandon a run.

**User-controlled parameters:** the end user can change the savings threshold, stop thresholds and pack-size tolerance in the advanced settings panel. Each has a default and a clamped range. The settings used are stored with each run so results are reproducible. Retry and backoff settings stay developer-side.

## Data layer

The database is Supabase Postgres, with plain SQL so the schema stays portable. There are no user accounts: runs are anonymous and shared through links.

| Table | Holds | Purpose |
| --- | --- | --- |
| `runs` | Form input, parameters used, each agent's output, status, tokens, cost, latency, Langfuse trace ID | Run page, trace panel, evals |
| `run_steps` | Per-step records: inputs, outputs, tool calls, tokens, latency | Public trace panel and SSE replay |
| `price_catalogue` | Store, product, brand, pack size, price, special and end date, timestamp, `data_source` | Seeded prices behind the lookup tool |
| `dish_cache` | Dish, normalised avoid text and prompt version to per-serving ingredients | Avoids repeat LLM calls for the same dish |
| `usage_counters` | Per-visitor and global daily token counts, day-stop flag | Guardrails |

**Caching:** only the dish expansion is cached, keyed on the dish, its normalised avoid text and the ingredient prompt version (a prompt change makes old rows miss), and a row is written only after the ingredient agent's output passes schema validation. The planner's output is never cached, because it depends on prices and parameters. Scaling by people and days is deterministic code.

**Access:** the browser never talks to Supabase directly. Only the backend does, using the service key. Row Level Security is enabled on every table anyway, so a leaked public key exposes nothing.

**Run links:** run IDs are long random values, not sequential numbers. The UI says that anyone with a link can view the run. Runs hold nothing personal; the free-text avoid field is the only place a user might type something sensitive, so a short notice sits beside it.

**Operations:** free Supabase projects can pause after a period of inactivity, which would break the demo. A scheduled keep-alive ping or health check prevents that, and the README documents it. Check the current free-tier terms before launch.

## Guardrails

Guardrails protect the free Gemini quota and keep a single run from costing too much. When the day's budget is used up, the service stops for the day and says so.

**Budget and day stop**

- The budget is counted in requests, not runs, because requests per day (500 on the free tier) is the binding limit and a five-meal run sends far more requests than a one-meal run. Tokens are still tracked for cost and traces.
- A run reserves an estimate based on its number of meals when it starts, and releases unused requests when it finishes. This stops ten simultaneous runs from overshooting the budget.
- New runs stop at the daily limit minus a buffer. Runs already started can use the buffer to finish.
- Once the cutoff is hit, the form is replaced by an "unavailable today" message with the reset time and a link to a sample run.
- If the buffer itself runs out mid-run, that run fails cleanly with a clear message.
- The internal budget is set below Google's current free-tier limits, which can change. A quota error (429) from Google flips the same day flag. Per-minute limits are watched too, since a burst of users can hit them before the daily limit.

**Per-visitor limits:** with no login, "per user" means an anonymous browser ID in a cookie plus an IP cap. This is not bulletproof, and the README names it as a known limitation.

**Input validation:** length limits and type checks on every field, and the ranges 1 to 12 people and 1 to 14 days, which also cap the cost of any single run.

**Prompt injection:** all form fields are treated as data, never as instructions. Every agent output is validated against its schema, and no agent has a tool that can do anything harmful. A visible "flagged input" path is out of scope for the demo.

## Observability and evals

Tracing shows what each run did, and evals show whether a prompt or model change made the agents better or worse. This is the part that most separates a demo from a production system.

**Tracing**

- Langfuse records each agent step: inputs, outputs, tool calls, tokens, latency and cost. It is the developer's debugging view.
- Run summaries and parameters live in the `runs` table, with the Langfuse trace ID stored on each run so the two link up.
- The public "how it worked" panel reads from the app's own `run_steps` records, not from Langfuse, so it works on shared links without exposing the tracing account.

**Evals:** about 30 to 40 cases, split by agent.

| Agent | What is checked | Method |
| --- | --- | --- |
| Ingredient | Key ingredients present, quantities in a sensible range, avoid list respected, schema valid | Deterministic checks; LLM judge only for fuzzy cases such as whether a potato salad list is sensible |
| Price | Matches are correct, exact vs equivalent vs substitute labelled properly | Deterministic checks against the seeded catalogue |
| Planner | Says "no meaningful difference" on near-identical seeded baskets; picks the right store when the gap is large | Deterministic checks on the verdict |

**When they run:** in CI on every prompt or model change, with scores shown in the README.

**Practical points**

- Evals use a separate API key or project, so CI cannot use up the demo's daily budget.
- The eval set stays small, because a full run on the free tier is slow.
- Google may use free-tier inputs to improve its products, so no real personal data goes into eval cases or traces.

## Frontend

The frontend is a React single-page app built with Vite, calling the FastAPI backend directly. Next.js is avoided because it adds a server layer that is not needed and makes SSE proxying harder.

**Screens**

1. **Form:** a repeatable meal card, an "add another meal" button, the pantry staples toggle, and a collapsed advanced settings panel
2. **Progress view:** the three agents in order, with the current step highlighted
3. **Result view:** the consolidated shopping list, a per-store basket comparison, and the planner's recommendation with reasoning, confidence and flags. A visible `data_source: seeded` label shows where prices come from.
4. **Run page:** the shareable link, which is the same result view, read-only. It doubles as the recorded-run demo.
5. **Unavailable today:** the message with the reset time and a link to a sample run

**Live progress with SSE:** the backend runs the pipeline as a background task and writes each step to the database. SSE only streams those events, so a closed tab or dropped connection does not stop the run. The event types are:

- `step_started`
- `step_completed` (with a short summary)
- `loopback`
- `run_completed`
- `run_failed`
- `unavailable`

On a reconnect, the client reloads the run's stored state and then resumes streaming. The chosen host must allow long-lived streaming responses, since some serverless platforms cut them off.

**Trace panel:** a simple "how it worked" panel on the result page shows each agent's inputs, outputs, tool calls, tokens and latency, read from `run_steps`.

## Configuration parameters

Every parameter has a default and a clamped range, and the values used are stored with each run. Only the daily token budget is still open, because it depends on the current Gemini limits.

| Parameter | Default | Range | User-editable |
| --- | --- | --- | --- |
| Savings threshold for a split | $8 or 8% of the basket, whichever is higher | $3 to $25, or 3% to 20% | Yes |
| Stop if items not found at any store | 25% | 10% to 40% | Yes |
| Stop if fewer items are comparable across all stores (`stop_comparable_pct`) | 50% | 40% to 80% | Yes |
| Pack-size tolerance (purchased quantities across stores, raises a `pack_gap` flag, never changes the tier) | 15% | 5% to 25% | Yes |
| People per meal | None | 1 to 12 | Form field |
| Days per meal | None | 1 to 14 | Form field |
| Include pantry staples | Off | On or off | Yes |
| Model | Gemini 3.1 Flash-Lite, free tier | Swappable | No |
| Daily budget | 70% of 500 requests/day = 350 requests | Fixed | No |
| Daily reset boundary | Midnight Pacific time, matching Google's quota reset | Fixed | No |
| In-flight buffer | 15% of the daily budget (about 52 requests) | Fixed | No |
| Retries | 20s timeout; timeouts/504 retried once; other transient errors 3 attempts with backoff 1s, 2s plus jitter; 429s honour retryDelay up to 20s | Fixed | No |
| Schema repair | 1 attempt when an agent's output fails validation | Fixed | No |
| Loop-back count | 1 | Fixed | No |

## Known limitations and what I would change at scale

| Limitation | Why it exists | What changes at scale |
| --- | --- | --- |
| Prices are seeded, not live | Scraping is brittle and may breach store terms | A live source behind the same lookup interface, with freshness checks |
| Per-visitor limits use a cookie and IP | No accounts, to keep friction low | Authenticated users and per-account quotas |
| Default model is 3.1 Flash-Lite, not 3.5 | 3.5 Flash-Lite had a transient outage at build time | the default model is chosen by eval score (step 11); the probe command (`python -m grocery_planner.llm.probe --model gemini-3.5-flash-lite`) re-checks a model with one request |
| One free-tier model provider | Cost | A swappable model layer already exists; add fallback providers and paid quota |
| Free-tier quota can run out for the day | Cost | Paid tier, request queue and priority for in-flight runs |
| Fixed agent sequence with one loop-back | Predictable and cheap | A supervisor that chooses steps, if evals show it helps |
| Small eval set of about 30 to 40 cases | Free-tier speed limits | Larger sets, production sampling and drift monitoring |
| Database connections from a single backend | Demo scale | Connection pooling and read replicas |
| Run links are public to anyone with the link | No accounts | Private runs and expiry |

## Open items to decide before building

- [ ] Read the Gemini 3.1 Flash-Lite limits from the AI Studio rate-limit page, then set the daily budget and buffer
- [x] Check Google's deprecations page for 2.5 Flash-Lite before building (done: 2.5 is limited to existing users, so the build uses 3.5)
- [ ] Hosting that allows long-lived streaming responses
- [ ] Whether the first seeded catalogue covers Aldi or only Coles and Woolworths
- [ ] The sample run shown on the unavailable-today screen
- [ ] The week-by-week build plan (drafted in the Build plan section; confirm the start date)

## Build plan

The build takes four weeks and runs bottom-up: the deterministic and testable parts first, the interface last. The backend is then built and tested without a UI, and evals exist before the frontend does.

&#91;embedded content: build plan · 4 weeks, 4 gates\]

**Why this order**

- The seeded catalogue and the planner's maths are the foundation, and they can be fully unit tested without calling any model.
- Evals arrive before the frontend, so prompt changes are measured from the start of the UI work.
- A run that works from the command line and stores its steps gives a fallback demo even if the frontend slips.

**Cut list if time runs short, in this order**

1. The advanced settings panel (the defaults stay)
2. Aldi in the seeded catalogue
3. The trace panel on the result page (Langfuse stays as the developer view)

**Throughout:** keep a short decision log of what failed and what you changed. Those entries become the interview stories.
