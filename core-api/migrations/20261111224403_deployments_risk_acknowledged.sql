-- migrate:up
-- Whether the caller accepted the deploy gate's warning (the run had no test dataset): recorded on the deployment.
ALTER TABLE deployments ADD COLUMN risk_acknowledged boolean NOT NULL DEFAULT false;

-- migrate:down
ALTER TABLE deployments DROP COLUMN risk_acknowledged;
