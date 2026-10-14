-- cost_usd stays what was actually charged (0 on the free tier). would_cost_usd is a
-- hypothetical: the run's tokens priced at the paid-tier rate of the model that served them.
ALTER TABLE runs ADD COLUMN would_cost_usd numeric(10, 6) NOT NULL DEFAULT 0;
