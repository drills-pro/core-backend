-- migrate:up
-- Training runs follow a dataset version and end in the job's report.json (docs/trainer-plan.md): the spec the
-- job got and the report it wrote are kept as they are. Existing runs (the Ludwig era) keep these NULL.
-- The CHECK lists mirror TrainPreset and TRAINABLE_TASK_TYPES (app/core/enums.py); a test fails if they drift.
ALTER TABLE training_runs
  ADD COLUMN dataset_version_id uuid REFERENCES dataset_versions(id),
  ADD COLUMN task text CHECK (task IN ('image-classification', 'object-detection')),
  ADD COLUMN preset text CHECK (preset IN ('small', 'medium', 'large')),
  ADD COLUMN spec jsonb,
  ADD COLUMN report jsonb;
CREATE INDEX training_runs_dataset_version_idx ON training_runs(dataset_version_id);

-- Purging a dataset removes its versions, so the runs that trained on them forget them, as they forget the dataset.
CREATE OR REPLACE FUNCTION purge_dataset_rows(p_dataset_id uuid, p_cutoff timestamptz) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
  -- SECURITY DEFINER bypasses RLS, so the caller's tenant is checked here: a bug elsewhere in core_app
  -- cannot purge another tenant's dataset.
  PERFORM 1 FROM datasets
    WHERE id = p_dataset_id AND deleted_at < p_cutoff AND tenant_id = current_setting('app.tenant_id', true)::uuid
    FOR UPDATE;
  IF NOT FOUND THEN
    RETURN false;
  END IF;
  UPDATE datasets SET published_version_id = NULL WHERE id = p_dataset_id;
  UPDATE dataset_imports SET base_version_id = NULL WHERE dataset_id = p_dataset_id;
  UPDATE projects SET default_test_dataset_id = NULL WHERE default_test_dataset_id = p_dataset_id;
  UPDATE training_runs SET dataset_id = NULL WHERE dataset_id = p_dataset_id;
  UPDATE training_runs SET test_dataset_id = NULL WHERE test_dataset_id = p_dataset_id;
  UPDATE training_runs SET dataset_version_id = NULL
    WHERE dataset_version_id IN (SELECT id FROM dataset_versions WHERE dataset_id = p_dataset_id);
  DELETE FROM dataset_versions WHERE dataset_id = p_dataset_id;
  DELETE FROM dataset_imports WHERE dataset_id = p_dataset_id;
  DELETE FROM datasets WHERE id = p_dataset_id;
  RETURN true;
END;
$$;

-- migrate:down
-- Restores purge_dataset_rows as 20261024052800_dataset_sweeps.sql defined it.
CREATE OR REPLACE FUNCTION purge_dataset_rows(p_dataset_id uuid, p_cutoff timestamptz) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
  PERFORM 1 FROM datasets
    WHERE id = p_dataset_id AND deleted_at < p_cutoff AND tenant_id = current_setting('app.tenant_id', true)::uuid
    FOR UPDATE;
  IF NOT FOUND THEN
    RETURN false;
  END IF;
  UPDATE datasets SET published_version_id = NULL WHERE id = p_dataset_id;
  UPDATE dataset_imports SET base_version_id = NULL WHERE dataset_id = p_dataset_id;
  UPDATE projects SET default_test_dataset_id = NULL WHERE default_test_dataset_id = p_dataset_id;
  UPDATE training_runs SET dataset_id = NULL WHERE dataset_id = p_dataset_id;
  UPDATE training_runs SET test_dataset_id = NULL WHERE test_dataset_id = p_dataset_id;
  DELETE FROM dataset_versions WHERE dataset_id = p_dataset_id;
  DELETE FROM dataset_imports WHERE dataset_id = p_dataset_id;
  DELETE FROM datasets WHERE id = p_dataset_id;
  RETURN true;
END;
$$;
DROP INDEX training_runs_dataset_version_idx;
ALTER TABLE training_runs DROP COLUMN report, DROP COLUMN spec, DROP COLUMN preset, DROP COLUMN task, DROP COLUMN dataset_version_id;
