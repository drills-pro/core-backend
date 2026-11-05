-- migrate:up
-- One live deployment per training run (and so per model version, which is 1:1 with its run): deploying, active and
-- stopped hold the run, failed and deleted release it. The index is what stops two concurrent creates, which read and
-- write in separate transactions. Duplicates that already exist keep only the newest; the older ones are soft-deleted
-- the way DELETE does it (predict key cleared), so the index can be built.
UPDATE deployments d SET status = 'deleted', predict_key_hash = NULL
WHERE d.status IN ('deploying', 'active', 'stopped')
  AND EXISTS (
    SELECT 1 FROM deployments n
    WHERE n.training_run_id = d.training_run_id AND n.status IN ('deploying', 'active', 'stopped')
      AND (n.created_at, n.id) > (d.created_at, d.id));
CREATE UNIQUE INDEX deployments_one_live_per_run ON deployments(training_run_id)
  WHERE status IN ('deploying', 'active', 'stopped');

-- POST .../deployments takes an Idempotency-Key like a training run does; scoped to the project. Rows made before it have none.
ALTER TABLE deployments ADD COLUMN idempotency_key text;
CREATE UNIQUE INDEX deployments_idempotency_key_idx ON deployments(project_id, idempotency_key)
  WHERE idempotency_key IS NOT NULL;

-- The most deployments a tenant may have serving (active or deploying; a stopped or deleted one frees its slot) per plan.
-- The default is the YAML fallback (serving.max_active_deployments_per_tenant) and the free tier.
ALTER TABLE plan_limits ADD COLUMN deployments jsonb NOT NULL
  DEFAULT '{"max_active_deployments": 3}'::jsonb CHECK (jsonb_typeof(deployments) = 'object');
UPDATE plan_limits SET deployments = '{"max_active_deployments": 25}'::jsonb WHERE plan_tier = 'pro';
UPDATE plan_limits SET deployments = '{"max_active_deployments": 100}'::jsonb WHERE plan_tier IN ('enterprise', 'internal');

-- migrate:down
ALTER TABLE plan_limits DROP COLUMN deployments;
DROP INDEX deployments_idempotency_key_idx;
ALTER TABLE deployments DROP COLUMN idempotency_key;
DROP INDEX deployments_one_live_per_run;
