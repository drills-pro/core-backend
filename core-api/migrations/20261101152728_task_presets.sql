-- migrate:up
-- What a training preset trains with (base model, learning rate, preprocessing, resources, hyperparameter defaults and
-- bounds) moves from app/core/task_catalog.py to rows, so a pinned revision bump or a new model is an admin call, not a
-- deploy. Platform data, not tenant data: no tenant_id, so no RLS; only /v1/admin routes write it.
-- A run copies the row into training_runs.spec when it is created, so editing a preset never changes a past run.
-- `name` is free text per task (the old small/medium/large are only the seed), so training_runs.preset loses its CHECK.
CREATE TABLE task_presets (
  task text NOT NULL,
  name text NOT NULL CHECK (name ~ '^[a-z0-9][a-z0-9-]{0,31}$'),
  base_model_repo text NOT NULL,
  base_model_revision text NOT NULL CHECK (base_model_revision ~ '^[0-9a-f]{40}$'),
  learning_rate double precision NOT NULL CHECK (learning_rate > 0),
  preprocessing jsonb NOT NULL,
  cpu double precision NOT NULL CHECK (cpu > 0),
  memory_bytes bigint NOT NULL CHECK (memory_bytes > 0),
  gpu double precision NOT NULL CHECK (gpu >= 0),
  max_runtime_s integer NOT NULL CHECK (max_runtime_s > 0),
  hyperparameters jsonb NOT NULL,
  updated_by text NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (task, name)
);
GRANT SELECT, INSERT, UPDATE, DELETE ON task_presets TO core_app;

ALTER TABLE training_runs DROP CONSTRAINT training_runs_preset_check;

INSERT INTO task_presets (task, name, base_model_repo, base_model_revision, learning_rate, preprocessing,
                          cpu, memory_bytes, gpu, max_runtime_s, hyperparameters, updated_by)
SELECT v.task, v.name, v.repo, v.revision, v.lr,
       CASE WHEN v.task = 'text-classification'
            THEN '{"max_length": 256}'::jsonb
            ELSE '{"mean": [0.485, 0.456, 0.406], "std": [0.229, 0.224, 0.225], "rescale": 0.00392156862745098, "resample": "bicubic"}'::jsonb END,
       8, 25769803776, 1, 14400,
       '{"epochs": {"default": 30, "min": 1, "max": 500}, "batch_size": {"default": 16, "min": 1, "max": 512}, "image_size": {"default": 224, "min": 32, "max": 1536}}'::jsonb,
       'migration'
FROM (VALUES
  ('image-classification', 'small', 'facebook/convnext-tiny-224', '6166b7613034066690a621d8bf25ffdf181a34f0', 1e-4),
  ('image-classification', 'medium', 'facebook/convnext-small-224', 'e0df91dcc78dbc0f56be8fbecff706128aee7c28', 1e-4),
  ('image-classification', 'large', 'facebook/convnext-base-224', '8550c1da008c99b90c9c42f1d4e1cb650d7a8c05', 5e-5),
  ('object-detection', 'small', 'PekingU/rtdetr_v2_r18vd', '5650961749fa93567c0d46fc7f43ea4f9e914107', 1e-4),
  ('object-detection', 'medium', 'PekingU/rtdetr_v2_r34vd', '5d60d8fb5eb2a3ee5575bf200ecbce4e2cfa420c', 1e-4),
  ('object-detection', 'large', 'PekingU/rtdetr_v2_r50vd', '282494075698cab9faa1096ae26856890030c817', 5e-5),
  ('text-classification', 'small', 'distilbert/distilbert-base-uncased', '12040accade4e8a0f71eabdb258fecc2e7e948be', 2e-5),
  ('text-classification', 'medium', 'google-bert/bert-base-uncased', '86b5e0934494bd15c9632b12f734a8a67f723594', 2e-5),
  ('text-classification', 'large', 'FacebookAI/roberta-base', 'e2da8e2f811d1448a5b465c236feacd80ffbac7b', 1e-5)
) AS v(task, name, repo, revision, lr);

-- migrate:down
DROP TABLE task_presets;
ALTER TABLE training_runs ADD CONSTRAINT training_runs_preset_check CHECK (preset IN ('small', 'medium', 'large'));
