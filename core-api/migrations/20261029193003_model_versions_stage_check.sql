-- migrate:up
-- stage mirrors ModelStage (app/integrations/data_plane/mlflow.py); core-api only ever writes these three. A test
-- fails if the CHECK drifts from the enum.
ALTER TABLE model_versions ADD CONSTRAINT model_versions_stage_check CHECK (stage IN ('None', 'Staging', 'Production'));

-- migrate:down
ALTER TABLE model_versions DROP CONSTRAINT model_versions_stage_check;
