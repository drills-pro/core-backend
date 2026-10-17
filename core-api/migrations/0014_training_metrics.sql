-- migrate:up
-- Surfaces the accuracy/loss (and detection mAP) already parsed from Ray
-- job logs (app/ray_client.py's parse_metrics) — previously computed only
-- to feed MLflow, now also persisted so the UI can show it directly.
ALTER TABLE training_runs ADD COLUMN IF NOT EXISTS metrics jsonb;

-- migrate:down
ALTER TABLE training_runs DROP COLUMN IF EXISTS metrics;
