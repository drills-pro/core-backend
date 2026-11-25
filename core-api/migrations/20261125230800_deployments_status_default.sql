-- migrate:up
-- The status column still defaults to 'deploying', a value the CHECK no longer allows, so an insert that leaves status out
-- fails. A deployment is active as soon as it exists.
ALTER TABLE deployments ALTER COLUMN status SET DEFAULT 'active';

-- migrate:down
ALTER TABLE deployments ALTER COLUMN status SET DEFAULT 'deploying';
