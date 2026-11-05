-- migrate:up
-- How a servable task is served moves from serve_client.SERVING to the task_types row: the encoder and postprocess functions
-- (names in serve_inputs.py / serve_postprocess.py) and the knobs they read. Both names are formatted into the script that runs
-- on the Ray cluster, so they are identifiers and nothing else.
ALTER TABLE task_types
  ADD COLUMN encoder text CHECK (encoder ~ '^[A-Za-z_][A-Za-z0-9_]*$'),
  ADD COLUMN postprocess text CHECK (postprocess ~ '^[A-Za-z_][A-Za-z0-9_]*$'),
  ADD COLUMN serving jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(serving) = 'object');

UPDATE task_types SET encoder = 'image_encoder', postprocess = 'classify' WHERE slug = 'image-classification';
UPDATE task_types SET encoder = 'image_encoder', postprocess = 'detect' WHERE slug = 'object-detection';
UPDATE task_types SET encoder = 'text_encoder', postprocess = 'classify' WHERE slug = 'text-classification';

ALTER TABLE task_types ADD CONSTRAINT task_types_servable_check CHECK (NOT servable OR (encoder IS NOT NULL AND postprocess IS NOT NULL));

-- migrate:down
ALTER TABLE task_types DROP CONSTRAINT task_types_servable_check;
ALTER TABLE task_types DROP COLUMN serving, DROP COLUMN postprocess, DROP COLUMN encoder;
