-- Reproducibility and lineage (improvement/phase2/05-reproducibility-lineage.md).
-- Additive, layers on top of 02/03/06/07/08's schema per 11-migration-phasing.md step 7.

-- FR4: runs reproduced from the same source share this so the UI can
-- cluster "same experiment, different config" instead of a flat list.
ALTER TABLE training_runs ADD COLUMN IF NOT EXISTS reproduction_group_id uuid;
CREATE INDEX IF NOT EXISTS training_runs_reproduction_group_idx ON training_runs(reproduction_group_id);

-- Pre-existing gap this doc's FR1/FR6 need fixed to work at all:
-- create_training_run has always accepted a dataset_id param (the real
-- dataset being trained on) but training_runs never actually stored it —
-- only dataset_version (a bare commit hash, ambiguous without knowing
-- which dataset's branch it belongs to). Additive, nullable — existing
-- rows just have no dataset linkage recorded, same as today.
ALTER TABLE training_runs ADD COLUMN IF NOT EXISTS dataset_id uuid REFERENCES datasets(id);
CREATE INDEX IF NOT EXISTS training_runs_dataset_id_idx ON training_runs(dataset_id);
