-- migrate:up
-- req/04 FR4: "configurable per tenant plan tier." Minimal real plan-tier
-- model: a column on tenants + a lookup used at deployment-creation time
-- to set the default rate limit (still overridable per-deployment).
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS plan_tier text NOT NULL DEFAULT 'free';
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_constraint WHERE conname = 'tenants_plan_tier_check') THEN
    ALTER TABLE tenants ADD CONSTRAINT tenants_plan_tier_check
      CHECK (plan_tier IN ('free', 'pro', 'enterprise'));
  END IF;
END
$$;

-- migrate:down
ALTER TABLE tenants DROP CONSTRAINT IF EXISTS tenants_plan_tier_check;
ALTER TABLE tenants DROP COLUMN IF EXISTS plan_tier;
