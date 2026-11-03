-- migrate:up
-- The /predict limits are per plan too: the biggest request body and the most requests in flight at once for one
-- deployment (per core-api replica). They start at the YAML values (serving.max_body_bytes is also the ceiling a tier may be
-- set to, as it is checked before the caller's plan is known), so nothing changes until an admin edits a tier.
ALTER TABLE plan_limits ADD COLUMN predict jsonb NOT NULL
  DEFAULT '{"max_body_bytes": 10485760, "max_inflight_per_deployment": 8}'::jsonb CHECK (jsonb_typeof(predict) = 'object');

-- migrate:down
ALTER TABLE plan_limits DROP COLUMN predict;
