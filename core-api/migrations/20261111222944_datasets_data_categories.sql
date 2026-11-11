-- migrate:up
-- What the customer declares a dataset holds (planning/18): stored only, so their own responsibility is on record.
-- Mirrors DataCategory in app/modules/user/datasets/constants.py; tests/modules/user/datasets keeps the two in step.
ALTER TABLE datasets ADD COLUMN data_categories text[] NOT NULL DEFAULT '{}'
  CONSTRAINT datasets_data_categories_check CHECK (data_categories <@ ARRAY['personal_data', 'biometric', 'other_sensitive']);

-- migrate:down
ALTER TABLE datasets DROP COLUMN data_categories;
