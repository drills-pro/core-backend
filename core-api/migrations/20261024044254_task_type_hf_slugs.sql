-- migrate:up
-- task_type becomes the Hugging Face task slug (hyphenated). legacy_untyped is gone; no production data
-- exists, so any such row just loses its type.
ALTER TABLE projects DROP CONSTRAINT projects_task_type_check;
UPDATE projects SET task_type = CASE task_type
  WHEN 'legacy_untyped' THEN NULL
  ELSE replace(task_type, '_', '-')
END;
ALTER TABLE projects ADD CONSTRAINT projects_task_type_check CHECK (task_type IN (
  'image-classification', 'object-detection', 'image-segmentation',
  'tabular-classification', 'tabular-regression',
  'text-classification', 'token-classification', 'question-answering', 'summarization', 'translation',
  'text-generation',
  'audio-classification', 'automatic-speech-recognition', 'text-to-speech'));

-- migrate:down
ALTER TABLE projects DROP CONSTRAINT projects_task_type_check;
UPDATE projects SET task_type = replace(task_type, '-', '_');
ALTER TABLE projects ADD CONSTRAINT projects_task_type_check CHECK (task_type IN (
  'image_classification', 'object_detection', 'tabular_classification', 'text_classification', 'legacy_untyped'));
