-- migrate:up
-- SPDX id of the base model's licence, as an admin recorded it when pinning the checkpoint; NULL = not recorded.
ALTER TABLE task_presets ADD COLUMN base_model_license text CHECK (base_model_license <> '');

-- migrate:down
ALTER TABLE task_presets DROP COLUMN base_model_license;
