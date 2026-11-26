-- migrate:up
-- What people see for a preset: a plain-language name and what it is for. Nullable: rows from before this change keep their
-- `name` alone, and the API falls back to it.
ALTER TABLE task_presets ADD COLUMN display_name text, ADD COLUMN description text;

-- migrate:down
ALTER TABLE task_presets DROP COLUMN description, DROP COLUMN display_name;
