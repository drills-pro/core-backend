-- migrate:up
-- A candidate runs a smoke job (a tiny run through the package's own entrypoints) when it is registered; an admin can
-- promote it to stable only after it passed. smoke_job_id is the Ray submission id, null until the job was submitted.
ALTER TABLE job_packages
  ADD COLUMN smoke_status text NOT NULL DEFAULT 'pending' CHECK (smoke_status IN ('pending', 'passed', 'failed')),
  ADD COLUMN smoke_job_id text;
UPDATE job_packages SET smoke_status = 'passed' WHERE status <> 'candidate';  -- promoted before the gate existed

-- migrate:down
ALTER TABLE job_packages DROP COLUMN smoke_job_id, DROP COLUMN smoke_status;
