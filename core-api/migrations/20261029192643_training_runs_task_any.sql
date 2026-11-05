-- migrate:up
-- Which tasks can be trained is decided in code (app/core/task_catalog.py), so adding one needs no migration:
-- training_runs.task accepts the whole taxonomy, exactly as projects.task_type does. A test fails if either CHECK
-- drifts from TaskType.
ALTER TABLE training_runs DROP CONSTRAINT training_runs_task_check;
ALTER TABLE training_runs ADD CONSTRAINT training_runs_task_check CHECK (task IN (
  'image-classification', 'object-detection', 'image-segmentation',
  'tabular-classification', 'tabular-regression',
  'text-classification', 'token-classification', 'question-answering', 'summarization', 'translation',
  'text-generation',
  'audio-classification', 'automatic-speech-recognition', 'text-to-speech'));

-- migrate:down
ALTER TABLE training_runs DROP CONSTRAINT training_runs_task_check;
ALTER TABLE training_runs ADD CONSTRAINT training_runs_task_check CHECK (task IN ('image-classification', 'object-detection'));
