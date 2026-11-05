-- migrate:up
-- 'internal' marks a staff test tenant on prod: its own plan limits, and left out of the back-office totals.
ALTER TABLE tenants DROP CONSTRAINT tenants_plan_tier_check;
ALTER TABLE tenants ADD CONSTRAINT tenants_plan_tier_check
  CHECK (plan_tier IN ('free', 'pro', 'enterprise', 'internal'));
ALTER TABLE plan_limits DROP CONSTRAINT plan_limits_plan_tier_check;
ALTER TABLE plan_limits ADD CONSTRAINT plan_limits_plan_tier_check
  CHECK (plan_tier IN ('free', 'pro', 'enterprise', 'internal'));

-- Starts as a copy of enterprise; tune it with PUT /v1/admin/plan-limits/internal.
INSERT INTO plan_limits (plan_tier, concurrency, archives, images, tables, huggingface, predict, updated_by)
SELECT 'internal', concurrency, archives, images, tables, huggingface, predict, 'migration'
FROM plan_limits WHERE plan_tier = 'enterprise';

CREATE OR REPLACE FUNCTION admin_platform_stats()
RETURNS TABLE(
  tenant_count bigint,
  tenant_count_30d bigint,
  project_count bigint,
  project_count_30d bigint,
  member_count bigint
)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  WITH customer AS (SELECT id, created_at FROM tenants WHERE plan_tier <> 'internal')
  SELECT (SELECT count(*) FROM customer),
         (SELECT count(*) FROM customer WHERE created_at >= now() - interval '30 days'),
         (SELECT count(*) FROM projects WHERE deleted_at IS NULL AND tenant_id IN (SELECT id FROM customer)),
         (SELECT count(*) FROM projects
           WHERE deleted_at IS NULL AND created_at >= now() - interval '30 days'
             AND tenant_id IN (SELECT id FROM customer)),
         (SELECT count(DISTINCT user_sub) FROM project_members WHERE tenant_id IN (SELECT id FROM customer));
$$;

-- migrate:down
CREATE OR REPLACE FUNCTION admin_platform_stats()
RETURNS TABLE(
  tenant_count bigint,
  tenant_count_30d bigint,
  project_count bigint,
  project_count_30d bigint,
  member_count bigint
)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT (SELECT count(*) FROM tenants),
         (SELECT count(*) FROM tenants WHERE created_at >= now() - interval '30 days'),
         (SELECT count(*) FROM projects WHERE deleted_at IS NULL),
         (SELECT count(*) FROM projects
           WHERE deleted_at IS NULL AND created_at >= now() - interval '30 days'),
         (SELECT count(DISTINCT user_sub) FROM project_members);
$$;

DELETE FROM plan_limits WHERE plan_tier = 'internal';
ALTER TABLE plan_limits DROP CONSTRAINT plan_limits_plan_tier_check;
ALTER TABLE plan_limits ADD CONSTRAINT plan_limits_plan_tier_check
  CHECK (plan_tier IN ('free', 'pro', 'enterprise'));
ALTER TABLE tenants DROP CONSTRAINT tenants_plan_tier_check;
ALTER TABLE tenants ADD CONSTRAINT tenants_plan_tier_check
  CHECK (plan_tier IN ('free', 'pro', 'enterprise'));
