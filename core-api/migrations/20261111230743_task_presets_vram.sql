-- migrate:up
-- GiB of GPU memory a run may hold; NULL = the GPU fraction alone decides.
ALTER TABLE task_presets ADD COLUMN vram_gb double precision CHECK (vram_gb > 0);

-- migrate:down
ALTER TABLE task_presets DROP COLUMN vram_gb;
