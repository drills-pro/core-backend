-- Pins a training run to the LakeFS commit (dataset version) it actually
-- trained on — NULL means "whatever was on the dataset's branch at
-- submission time" (see app/training.py, app/lakefs_client.py).
ALTER TABLE training_runs ADD COLUMN IF NOT EXISTS dataset_version text;
