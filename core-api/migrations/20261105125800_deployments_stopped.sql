-- migrate:up
-- A deployment can be stopped (not served, predict key kept, can be started again) and deleted (soft: the row stays for
-- lineage, its predict key is cleared). The init migration left the status CHECK unnamed; this names it.
ALTER TABLE deployments DROP CONSTRAINT deployments_status_check;
ALTER TABLE deployments ADD CONSTRAINT deployments_status_check
  CHECK (status IN ('deploying', 'active', 'stopped', 'failed', 'deleted'));

-- migrate:down
ALTER TABLE deployments DROP CONSTRAINT deployments_status_check;
ALTER TABLE deployments ADD CONSTRAINT deployments_status_check
  CHECK (status IN ('deploying', 'active', 'failed', 'deleted'));
