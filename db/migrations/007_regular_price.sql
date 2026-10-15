-- Specials run Wednesday to Tuesday. regular_price is the shelf price once a special is over,
-- so the planner can ask what the verdict would be without the specials. Set for every product
-- on special (the seed loader enforces that; existing rows are filled by re-seeding).
ALTER TABLE price_catalogue ADD COLUMN regular_price numeric(10, 2);
ALTER TABLE price_catalogue ADD CONSTRAINT price_catalogue_regular_price_check
    CHECK (regular_price IS NULL OR (on_special AND regular_price > price));
