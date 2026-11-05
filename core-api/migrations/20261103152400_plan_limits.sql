-- migrate:up
-- Per-plan limits move from config/base.yaml (dataset_limits, ray.max_concurrent_training_runs) to one row per plan tier,
-- one jsonb object per group of related limits (shapes: app/core/plan_limits.py). The YAML values stay as the platform
-- ceiling for training concurrency (the queue horizon is computed from it) and as the fallback for a tier without a row;
-- job timeout and Ray sizing stay global. Rows start at today's YAML values, so nothing changes until an admin edits one.
-- Platform data: no tenant_id, no RLS, written only through /v1/admin/plan-limits.
CREATE TABLE plan_limits (
  plan_tier text PRIMARY KEY CHECK (plan_tier IN ('free', 'pro', 'enterprise')),
  concurrency jsonb NOT NULL CHECK (jsonb_typeof(concurrency) = 'object'),
  archives jsonb NOT NULL CHECK (jsonb_typeof(archives) = 'object'),
  images jsonb NOT NULL CHECK (jsonb_typeof(images) = 'object'),
  tables jsonb NOT NULL CHECK (jsonb_typeof(tables) = 'object'),
  huggingface jsonb NOT NULL CHECK (jsonb_typeof(huggingface) = 'object'),
  updated_by text NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);
GRANT SELECT, INSERT, UPDATE ON plan_limits TO core_app;

INSERT INTO plan_limits (plan_tier, concurrency, archives, images, tables, huggingface, updated_by)
SELECT tier,
       '{"max_active_imports_per_tenant": 3, "max_concurrent_training_runs": 3}'::jsonb,
       '{"upload_max_bytes": 2147483648, "extracted_max_bytes": 10737418240, "max_files": 100000}'::jsonb,
       '{"image_max_dimension": 8192, "image_max_bytes": 26214400}'::jsonb,
       '{"csv_max_rows": 5000000, "csv_max_columns": 1000}'::jsonb,
       '{"hf_max_repo_bytes": 10737418240}'::jsonb,
       'migration'
FROM unnest(ARRAY['free', 'pro', 'enterprise']) AS tier;

-- migrate:down
DROP TABLE plan_limits;
