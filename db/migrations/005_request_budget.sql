-- usage_counters now counts REQUESTS (what the Gemini free tier limits), one row per model.
-- global rows: (day, 'global', 'global', model)  used / reserved requests and the day-stop flag
-- visitor rows: (day, 'visitor', visitor_id, '')   used = runs started that day
-- ip rows:      (day, 'ip', sha256(ip + daily salt), '')   the raw IP is never stored
-- day is the America/Los_Angeles date (Google's quota resets at midnight Pacific).
ALTER TABLE usage_counters RENAME COLUMN tokens_used TO used;
ALTER TABLE usage_counters RENAME COLUMN tokens_reserved TO reserved;
ALTER TABLE usage_counters ADD COLUMN model text NOT NULL DEFAULT '';
-- Why a model's day_stopped flag is set: day_budget (our counter) or google_quota (a 429).
ALTER TABLE usage_counters ADD COLUMN stop_reason text;
ALTER TABLE usage_counters DROP CONSTRAINT usage_counters_pkey;
ALTER TABLE usage_counters ADD PRIMARY KEY (day, scope, scope_key, model);
ALTER TABLE usage_counters ADD CONSTRAINT usage_counters_nonneg CHECK (used >= 0 AND reserved >= 0);

-- The part of a run's reservation that its requests have not used yet, and the Pacific day it
-- was taken on (the run may finish after midnight).
ALTER TABLE runs ADD COLUMN reserved_remaining integer NOT NULL DEFAULT 0;
ALTER TABLE runs ADD COLUMN reservation_day date;
