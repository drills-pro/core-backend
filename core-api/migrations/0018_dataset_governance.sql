-- Dataset governance (improvement/phase2/03-dataset-governance.md).
-- Additive per 11-migration-phasing.md step 2.

-- FR5: a project can designate a default test dataset that pre-fills
-- every training run's test-set picker.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS default_test_dataset_id uuid REFERENCES datasets(id);
