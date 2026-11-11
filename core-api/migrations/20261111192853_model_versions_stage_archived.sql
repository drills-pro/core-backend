-- migrate:up
-- A retired version moves to Archived (MLflow's own stage); stage mirrors ModelStage and a test fails on drift.
ALTER TABLE model_versions DROP CONSTRAINT model_versions_stage_check;
ALTER TABLE model_versions ADD CONSTRAINT model_versions_stage_check CHECK (stage IN ('None', 'Staging', 'Production', 'Archived'));

-- migrate:down
ALTER TABLE model_versions DROP CONSTRAINT model_versions_stage_check;
ALTER TABLE model_versions ADD CONSTRAINT model_versions_stage_check CHECK (stage IN ('None', 'Staging', 'Production'));
