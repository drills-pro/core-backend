-- migrate:up
-- A separately versioned, user-designated held-out test set (distinct
-- from the training dataset) — see app/datasets.py's dataset CRUD (any
-- dataset can serve as a test set) and app/ray_client.py's explicit
-- training_set/test_set wiring, replacing the internal random split
-- when set.
ALTER TABLE training_runs ADD COLUMN IF NOT EXISTS test_dataset_id uuid REFERENCES datasets(id);
ALTER TABLE training_runs ADD COLUMN IF NOT EXISTS test_dataset_version text;

-- migrate:down
ALTER TABLE training_runs DROP COLUMN IF EXISTS test_dataset_version;
ALTER TABLE training_runs DROP COLUMN IF EXISTS test_dataset_id;
