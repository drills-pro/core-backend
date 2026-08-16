-- req/04 FR4: "Rate limiting on the inference endpoint per deployment,
-- configurable per tenant plan tier." Per-deployment for now (plan-tier
-- defaults aren't modeled yet — no plan/tier table exists).
ALTER TABLE deployments ADD COLUMN IF NOT EXISTS rate_limit_per_minute integer NOT NULL DEFAULT 60;
