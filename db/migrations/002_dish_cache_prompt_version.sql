-- dish_cache rows now depend on the ingredient agent's prompt: when the prompt changes, its
-- version string changes and old rows simply miss (they are never read or deleted).
-- Existing rows get version '0', which no prompt version uses.
ALTER TABLE dish_cache ADD COLUMN prompt_version text NOT NULL DEFAULT '0';
ALTER TABLE dish_cache ALTER COLUMN prompt_version DROP DEFAULT;
ALTER TABLE dish_cache DROP CONSTRAINT dish_cache_pkey;
ALTER TABLE dish_cache ADD PRIMARY KEY (dish_key, avoid_key, prompt_version);
