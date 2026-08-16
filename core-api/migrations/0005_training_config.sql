-- FR1 (06-ml-training-mlflow-ludwig.md, Ludwig section): the
-- platform-generated config must be stored alongside the model version,
-- for reproducibility and "export config". Run after 0004.
ALTER TABLE training_runs ADD COLUMN IF NOT EXISTS config jsonb NOT NULL DEFAULT '{}'::jsonb;
