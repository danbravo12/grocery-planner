-- Grocery Planner initial schema. Plain Postgres (runs on Supabase).
-- Columns marked "inferred" are not spelled out in the design doc; they are the simplest
-- fields needed to do the job the doc describes.

-- runs: one row per planner run. Anonymous; shared by link.
CREATE TABLE runs (
    -- uuid v4 = 122 random bits, so run links cannot be guessed or enumerated.
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at        timestamptz NOT NULL DEFAULT now(),
    finished_at       timestamptz,                       -- inferred
    status            text NOT NULL DEFAULT 'queued'
                      CHECK (status IN ('queued', 'running', 'completed', 'stopped', 'failed')),
    -- 'stopped' = orchestrator stop thresholds hit; 'failed' = error or budget exhausted.
    stop_reason       text,                              -- inferred: why a run stopped/failed
    input             jsonb NOT NULL,                    -- form input (meals[], pantry toggle)
    parameters        jsonb NOT NULL,                    -- config used, for reproducibility
    ingredient_output jsonb,
    price_output      jsonb,
    planner_output    jsonb,
    tokens_in         integer NOT NULL DEFAULT 0,        -- inferred: split in/out
    tokens_out        integer NOT NULL DEFAULT 0,
    cost_usd          numeric(10, 6) NOT NULL DEFAULT 0, -- 0 on the free tier
    latency_ms        integer,
    langfuse_trace_id text
);

-- run_steps: per-step records behind the public trace panel and SSE replay.
CREATE TABLE run_steps (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id      uuid NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    seq         integer NOT NULL,                        -- inferred: order within the run
    agent       text NOT NULL CHECK (agent IN ('ingredient', 'price', 'planner')),
    attempt     integer NOT NULL DEFAULT 1,              -- inferred: 2 = the planner loop-back pass
    status      text NOT NULL DEFAULT 'running'
                CHECK (status IN ('running', 'completed', 'failed')),
    input       jsonb,
    output      jsonb,
    tool_calls  jsonb NOT NULL DEFAULT '[]'::jsonb,
    tokens_in   integer NOT NULL DEFAULT 0,
    tokens_out  integer NOT NULL DEFAULT 0,
    latency_ms  integer,
    error       text,                                    -- inferred
    started_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, seq)
);

-- price_catalogue: seeded prices behind the price lookup tool.
CREATE TABLE price_catalogue (
    -- store is plain text with no CHECK so Aldi can be added by data only.
    store            text NOT NULL,
    product_id       text NOT NULL,
    product_name     text NOT NULL,
    brand            text NOT NULL,
    brand_label      text NOT NULL CHECK (brand_label IN ('house_brand', 'branded')),
    category         text NOT NULL,
    search_terms     text[] NOT NULL DEFAULT '{}',
    pack_size        numeric(10, 3) NOT NULL CHECK (pack_size > 0),
    pack_unit        text NOT NULL CHECK (pack_unit IN ('g', 'ml', 'each')),
    price            numeric(10, 2) NOT NULL CHECK (price > 0),  -- current price (special price if on special)
    -- inferred: derived in the database so it can never disagree with price/pack_size.
    -- Per 100g / 100ml, or per item for 'each'. Matches CatalogueProduct.unit_price.
    unit_price       numeric(12, 4) GENERATED ALWAYS AS (
                         round(price / pack_size * CASE WHEN pack_unit = 'each' THEN 1 ELSE 100 END, 4)
                     ) STORED,
    on_special       boolean NOT NULL DEFAULT false,
    special_end_date date,
    price_timestamp  timestamptz NOT NULL,
    data_source      text NOT NULL DEFAULT 'seeded',
    quality_band     text NOT NULL CHECK (quality_band IN ('budget', 'standard', 'premium')),
    PRIMARY KEY (store, product_id),
    CHECK (special_end_date IS NULL OR on_special)
);

-- dish_cache: (dish, avoid) -> per-serving ingredients, so repeat dishes skip the LLM.
-- Rows are only written after the ingredient agent's output passes schema validation.
-- Both key parts are normalised by make_dish_cache_key() (lowercase, trimmed, whitespace
-- collapsed; avoid is split on commas, sorted and rejoined). avoid_key is '' for no avoid.
CREATE TABLE dish_cache (
    dish_key    text NOT NULL,                           -- inferred
    avoid_key   text NOT NULL DEFAULT '',                -- inferred
    dish_name   text NOT NULL,
    ingredients jsonb NOT NULL,                          -- per-serving ingredient list
    model       text,                                    -- inferred: which model produced it
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (dish_key, avoid_key)
);

-- usage_counters: daily token counts per scope, plus the day-stop flag.
-- inferred layout: one row per (day, scope, scope_key). The global row
-- (scope 'global', scope_key 'global') carries day_stopped. 'day' is the
-- Pacific-time date, matching Google's quota reset.
CREATE TABLE usage_counters (
    day             date NOT NULL,
    scope           text NOT NULL CHECK (scope IN ('global', 'visitor', 'ip')),
    scope_key       text NOT NULL,
    tokens_used     bigint NOT NULL DEFAULT 0,
    tokens_reserved bigint NOT NULL DEFAULT 0,           -- in-flight reservations (see Guardrails)
    day_stopped     boolean NOT NULL DEFAULT false,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (day, scope, scope_key)
);

-- Row Level Security on every table, deliberately with NO policies: anon/authenticated
-- keys can read and write nothing. The backend uses the service key, which bypasses RLS.
ALTER TABLE runs            ENABLE ROW LEVEL SECURITY;
ALTER TABLE run_steps       ENABLE ROW LEVEL SECURITY;
ALTER TABLE price_catalogue ENABLE ROW LEVEL SECURITY;
ALTER TABLE dish_cache      ENABLE ROW LEVEL SECURITY;
ALTER TABLE usage_counters  ENABLE ROW LEVEL SECURITY;
