-- A usage counter: one row per browser, found by an HMAC of its visitor cookie id (the secret is
-- VISITOR_HASH_SECRET; the raw id and the IP are never stored). The table deliberately has no
-- run_id and nothing in it points at a run, so no run record holds a visitor id.
-- Counting starts when this migration is deployed; there is no backfill.
CREATE TABLE visitors (
    visitor_hash text PRIMARY KEY,
    first_seen   timestamptz NOT NULL DEFAULT now(),
    last_seen    timestamptz NOT NULL DEFAULT now(),
    run_count    integer NOT NULL DEFAULT 0 CHECK (run_count >= 0),  -- runs admitted
    is_owner     boolean NOT NULL DEFAULT false                      -- carries the owner cookie
);
ALTER TABLE visitors ENABLE ROW LEVEL SECURITY;  -- no policies, like every other table

-- Whether the run was started from the owner's browser, so the stats can leave it out. This is a
-- flag on the run, not a visitor id: it does not say whose browser (or which one) it was.
ALTER TABLE runs ADD COLUMN is_owner boolean NOT NULL DEFAULT false;
