-- Which model answered, and whether the fallback model was needed (LLMResult.model / .fell_back).
-- Per step, so the public trace panel and evals can say which model produced each output.
ALTER TABLE run_steps ADD COLUMN model text;
ALTER TABLE run_steps ADD COLUMN fell_back boolean NOT NULL DEFAULT false;
-- Distinct models that answered during the run, in order of first use.
ALTER TABLE runs ADD COLUMN models text[] NOT NULL DEFAULT '{}';
