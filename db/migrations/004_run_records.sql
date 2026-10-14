-- Run and step records written by the orchestrator (the spec called this "migration 003"; that
-- number was already taken by 003_record_answering_model.sql).
-- The tables are empty at this point, so the CHECK changes need no data fix-ups.

-- runs ----------------------------------------------------------------------------------
ALTER TABLE runs RENAME COLUMN models TO models_used;        -- models that answered, in order
ALTER TABLE runs RENAME COLUMN finished_at TO completed_at;  -- set for completed, stopped, failed
-- stop_reason: why a run STOPPED (too much missing). failure_reason: why it FAILED
-- (daily_quota, rate_limited, model_unavailable, schema_validation, model_error, timeout,
-- stale, internal). The day-stop logic reads failure_reason = 'daily_quota'.
ALTER TABLE runs ADD COLUMN failure_reason text;
ALTER TABLE runs ADD COLUMN user_message text;               -- plain sentence shown to the user
ALTER TABLE runs ADD COLUMN fell_back boolean NOT NULL DEFAULT false;
ALTER TABLE runs ADD COLUMN requests integer NOT NULL DEFAULT 0;           -- what the budget counts
ALTER TABLE runs ADD COLUMN estimated_requests integer NOT NULL DEFAULT 0; -- worst case reserved
ALTER TABLE runs ADD COLUMN thinking_tokens integer NOT NULL DEFAULT 0;
ALTER TABLE runs ADD COLUMN loopback_used boolean NOT NULL DEFAULT false;
ALTER TABLE runs ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now(); -- heartbeat
CREATE INDEX runs_running_updated_idx ON runs (updated_at) WHERE status = 'running';

-- run_steps -----------------------------------------------------------------------------
-- name says which step this row is; agent (nullable) says which agent ran, when one did.
ALTER TABLE run_steps ALTER COLUMN agent DROP NOT NULL;
ALTER TABLE run_steps DROP CONSTRAINT run_steps_agent_check;
ALTER TABLE run_steps ADD COLUMN name text NOT NULL DEFAULT '';
ALTER TABLE run_steps ALTER COLUMN name DROP DEFAULT;
ALTER TABLE run_steps ADD CONSTRAINT run_steps_name_check
    CHECK (name IN ('ingredient', 'price', 'planner', 'reprice', 'planner_2', 'stop'));
ALTER TABLE run_steps ADD COLUMN requests integer NOT NULL DEFAULT 0;
ALTER TABLE run_steps ADD COLUMN thinking_tokens integer NOT NULL DEFAULT 0;
ALTER TABLE run_steps ADD COLUMN summary text;               -- short line for the progress view
ALTER TABLE run_steps ADD COLUMN finished_at timestamptz;
