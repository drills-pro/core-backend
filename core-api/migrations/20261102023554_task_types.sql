-- migrate:up
-- What the platform can do with each task type moves from app/core/task_catalog.py to rows: the kind of data, whether it can
-- be trained and served, and the trainer that trains it, so enabling a task is an admin call, not a deploy. The slug set
-- stays the Hugging Face taxonomy (TaskType); projects.task_type and training_runs.task keep their wide CHECKs.
-- Platform data, not tenant data: no tenant_id, so no RLS; only /v1/admin routes write it.
CREATE TABLE task_types (
  slug text PRIMARY KEY,
  data_kind text NOT NULL CHECK (data_kind IN ('images', 'tabular', 'text')),
  trainer text CHECK (trainer ~ '^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$'),
  servable boolean NOT NULL DEFAULT false,
  enabled boolean NOT NULL DEFAULT true,
  updated_by text NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);
GRANT SELECT, INSERT, UPDATE ON task_types TO core_app;

INSERT INTO task_types (slug, data_kind, trainer, servable, updated_by) VALUES
  ('image-classification', 'images', 'ray_training.classification:train', true, 'migration'),
  ('object-detection', 'images', 'ray_training.detection:train', true, 'migration'),
  ('text-classification', 'text', 'ray_training.text_classification:train', true, 'migration'),
  ('tabular-classification', 'tabular', NULL, false, 'migration'),
  ('tabular-regression', 'tabular', NULL, false, 'migration');

ALTER TABLE task_presets ADD CONSTRAINT task_presets_task_fkey FOREIGN KEY (task) REFERENCES task_types (slug);

-- migrate:down
ALTER TABLE task_presets DROP CONSTRAINT task_presets_task_fkey;
DROP TABLE task_types;
