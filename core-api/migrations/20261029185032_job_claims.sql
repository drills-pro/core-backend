-- migrate:up
-- Background reconcilers share their work across replicas (app/integrations/job_lifecycle.py): a replica leases a
-- row for a minute with claimed_until (FOR UPDATE SKIP LOCKED), polls Ray with no connection held, then moves the
-- row with a compare-and-set. The lease is for crash recovery only; correctness never depends on it.
ALTER TABLE training_runs ADD COLUMN claimed_until timestamptz;
ALTER TABLE dataset_imports ADD COLUMN claimed_until timestamptz;

-- A deployment has no completed_at and its created_at is old after a 'main' redeploy (the same row gets a new
-- ray_job_id and goes back to deploying), so the deploy deadline counts from deploying_since: restarted below
-- whenever the job id changes, so no caller has to remember to.
ALTER TABLE deployments
  ADD COLUMN claimed_until timestamptz,
  ADD COLUMN deploying_since timestamptz NOT NULL DEFAULT now();

CREATE FUNCTION deployments_restart_deploy_clock() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  NEW.deploying_since := now();
  RETURN NEW;
END;
$$;

CREATE TRIGGER deployments_restart_deploy_clock
  BEFORE UPDATE OF ray_job_id ON deployments
  FOR EACH ROW WHEN (OLD.ray_job_id IS DISTINCT FROM NEW.ray_job_id)
  EXECUTE FUNCTION deployments_restart_deploy_clock();

REVOKE ALL ON FUNCTION deployments_restart_deploy_clock() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION deployments_restart_deploy_clock() TO core_app;

-- The claim and inflight_deploy_tenant_ids() filter on status = 'deploying'; training_runs and dataset_imports already
-- have a partial index on tenant_id for their in-flight statuses, deployments did not (seq scan at 50k rows).
CREATE INDEX deployments_deploying_idx ON deployments(tenant_id) WHERE status = 'deploying';

-- migrate:down
DROP INDEX deployments_deploying_idx;
DROP TRIGGER deployments_restart_deploy_clock ON deployments;
DROP FUNCTION deployments_restart_deploy_clock();
ALTER TABLE deployments DROP COLUMN deploying_since, DROP COLUMN claimed_until;
ALTER TABLE dataset_imports DROP COLUMN claimed_until;
ALTER TABLE training_runs DROP COLUMN claimed_until;
