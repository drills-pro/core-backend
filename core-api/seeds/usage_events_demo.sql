-- Demo data for the usage ledger: one row of each resource type, enough
-- to see every kind of row the activity log renders.
--
-- Not a migration, and not to be run anywhere but a dev database: it
-- invents people, projects and usage. Needs 0039 applied first. Run as
-- the postgres superuser, which bypasses RLS and the INSERT-only grant:
--
--   docker exec -i <postgres-container> psql -U postgres -d core < core-api/seeds/usage_events_demo.sql
--
-- Safe to re-run: every source_ref is fixed, so the ledger's own
-- idempotency key (resource_type, source_ref) turns a repeat into a
-- no-op. Everything it writes is tagged `seed:` in source_ref and `seed-`
-- in user and project names, so it can be found again.
--
-- One row per question 0039 was written for:
--
--   GPU-seconds per user   training, inference   (gpu_seconds, per person)
--   storage per user       storage_dataset, storage_model   (bytes)
--   GPU health             gpu_utilization (percent), vram_usage (bytes),
--                          platform rows with a device and no tenant

BEGIN;

INSERT INTO users (zitadel_user_id, username, display_name) VALUES
  ('seed-alice', 'seed-alice', 'Alice Chen'),
  ('seed-dao',   'seed-dao',   'Dao Wongsa')
ON CONFLICT (zitadel_user_id) DO NOTHING;

INSERT INTO projects (id, tenant_id, name, task_type) VALUES
  ('5eed0000-0000-4000-8000-000000000001', '11111111-1111-1111-1111-111111111111', 'seed-defect-detection', 'object_detection'),
  ('5eed0000-0000-4000-8000-000000000003', '22222222-2222-2222-2222-222222222222', 'seed-ticket-router', 'text_classification')
ON CONFLICT (id) DO NOTHING;

INSERT INTO usage_events
  (tenant_id, project_id, user_id, device, resource_type, quantity, unit, occurred_at, source_ref, pricing_version)
VALUES
  -- A 2-hour training run.
  ('11111111-1111-1111-1111-111111111111', '5eed0000-0000-4000-8000-000000000001', 'seed-alice', NULL,
   'training', 7200, 'gpu_seconds', now() - interval '5 hours', 'seed:training:1', 'v1-raw-gpu-seconds'),
  -- A day's inference, rolled up.
  ('22222222-2222-2222-2222-222222222222', '5eed0000-0000-4000-8000-000000000003', 'seed-dao', NULL,
   'inference', 1450.5, 'gpu_seconds', now() - interval '4 hours', 'seed:inference:1', 'v1-raw-gpu-seconds'),
  -- Daily storage snapshots: 40 GB of datasets, 3.2 GB of models.
  ('11111111-1111-1111-1111-111111111111', '5eed0000-0000-4000-8000-000000000001', 'seed-alice', NULL,
   'storage_dataset', 42949672960, 'bytes', now() - interval '3 hours', 'seed:storage_dataset:1', 'v1-raw-gpu-seconds'),
  ('22222222-2222-2222-2222-222222222222', '5eed0000-0000-4000-8000-000000000003', 'seed-dao', NULL,
   'storage_model', 3435973837, 'bytes', now() - interval '2 hours', 'seed:storage_model:1', 'v1-raw-gpu-seconds'),
  -- One GPU's health: 87% busy, 21 GB of its 24 GB VRAM in use.
  (NULL, NULL, NULL, 'gpu-node-1/0',
   'gpu_utilization', 87, 'percent', now() - interval '1 hour', 'seed:gpu_utilization:1', 'not-billed'),
  (NULL, NULL, NULL, 'gpu-node-1/0',
   'vram_usage', 22548578304, 'bytes', now() - interval '1 hour', 'seed:vram_usage:1', 'not-billed')
ON CONFLICT (resource_type, source_ref) DO NOTHING;

COMMIT;
