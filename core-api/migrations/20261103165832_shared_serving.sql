-- migrate:up
-- Deployments are served by two long-lived Serve apps (ray_serving package) that load models by id, so a new deployment
-- starts no Ray job: ray_job_id is null for it. `serving` tells the two ways apart; rows that exist now are `legacy`
-- (one Serve app of their own at route_prefix) until they are re-deployed.
ALTER TABLE job_packages DROP CONSTRAINT job_packages_name_check;
ALTER TABLE job_packages ADD CONSTRAINT job_packages_name_check CHECK (name IN ('ray_jobs', 'ray_training', 'ray_serving'));

ALTER TABLE deployments ALTER COLUMN ray_job_id DROP NOT NULL;
ALTER TABLE deployments ADD COLUMN serving text NOT NULL DEFAULT 'legacy' CONSTRAINT deployments_serving_check CHECK (serving IN ('legacy', 'shared'));

-- migrate:down
ALTER TABLE deployments DROP COLUMN serving;
UPDATE deployments SET ray_job_id = '' WHERE ray_job_id IS NULL;
ALTER TABLE deployments ALTER COLUMN ray_job_id SET NOT NULL;
ALTER TABLE job_packages DROP CONSTRAINT job_packages_name_check;
ALTER TABLE job_packages ADD CONSTRAINT job_packages_name_check CHECK (name IN ('ray_jobs', 'ray_training'));
