-- usage_events learns who used a resource, and starts carrying GPU health.
--
-- Three questions the ledger could not answer before:
--
--   GPU utilisation and VRAM, to see whether the cluster is healthy
--   storage, overall and per user
--   GPU-seconds per user, training and inference apart, to see who uses
--     the most and to bill or enforce a quota on it
--
-- Per user: a `user_id` column, the Zitadel subject of whoever the usage
-- is charged to (users.zitadel_user_id). Not a foreign key on purpose —
-- users only gets a row once someone has a profile (0020), and a metering
-- write must never fail because the person behind it has not opened one.
-- Null on rows written before this migration, and on platform rows.
--
-- GPU health: two new resource types, sampled per GPU.
--
--   gpu_utilization  quantity in percent (0–100)
--   vram_usage       quantity in bytes in use
--
-- These are gauges — a reading at one instant — not consumption, so they
-- are never summed into a bill: they carry pricing_version 'not-billed',
-- and they belong to the platform rather than a tenant, so tenant_id and
-- project_id are null. The tenant_isolation policy (0003) compares
-- tenant_id to app.tenant_id, which a null never equals, so no tenant
-- connection ever sees these rows and the per-tenant rollups in
-- app/modules/user/usage keep summing only what that tenant used. Only
-- the back-office reader below, which is SECURITY DEFINER, returns them.
--
-- `device` names the GPU a gauge row was read from ("gpu-node-1/0"), so
-- that does not have to be parsed back out of source_ref.

ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS user_id text;
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS device text;
CREATE INDEX IF NOT EXISTS usage_events_user_idx ON usage_events(user_id, occurred_at DESC);

ALTER TABLE usage_events DROP CONSTRAINT IF EXISTS usage_events_resource_type_check;
ALTER TABLE usage_events ADD CONSTRAINT usage_events_resource_type_check CHECK (
  resource_type IN (
    'training', 'storage_dataset', 'storage_model', 'inference',
    'gpu_utilization', 'vram_usage'
  )
);

ALTER TABLE usage_events ALTER COLUMN tenant_id DROP NOT NULL;
ALTER TABLE usage_events ALTER COLUMN project_id DROP NOT NULL;
-- Only a gauge may belong to nobody: every billable row still names the
-- tenant and project it is charged to, exactly as before.
ALTER TABLE usage_events DROP CONSTRAINT IF EXISTS usage_events_owner_check;
ALTER TABLE usage_events ADD CONSTRAINT usage_events_owner_check CHECK (
  resource_type IN ('gpu_utilization', 'vram_usage')
  OR (tenant_id IS NOT NULL AND project_id IS NOT NULL)
);
ALTER TABLE usage_events DROP CONSTRAINT IF EXISTS usage_events_percent_check;
ALTER TABLE usage_events ADD CONSTRAINT usage_events_percent_check CHECK (
  unit <> 'percent' OR quantity <= 100
);

-- The back-office reader (0038) gains the new columns, and stops dropping
-- rows that name no tenant: its JOIN on tenants becomes a LEFT JOIN.
--
-- Dropped rather than replaced: CREATE OR REPLACE cannot change a
-- function's return type.
DROP FUNCTION IF EXISTS admin_usage_events(int, int, uuid, text, text, timestamptz);

CREATE FUNCTION admin_usage_events(
  p_limit int,
  p_offset int,
  p_tenant_id uuid,
  p_resource_type text,
  p_q text,
  p_since timestamptz
)
RETURNS TABLE(
  id bigint,
  tenant_id uuid,
  tenant_name text,
  project_id uuid,
  project_name text,
  user_id text,
  -- users.display_name, falling back to the username; null when users has
  -- no row for the subject, and the caller shows user_id instead.
  user_name text,
  device text,
  resource_type text,
  quantity numeric,
  unit text,
  occurred_at timestamptz,
  source_ref text,
  pricing_version text,
  total bigint
)
LANGUAGE sql SECURITY DEFINER AS $$
  WITH matching AS (
    SELECT u.id, u.tenant_id, t.name AS tenant_name,
           u.project_id, p.name AS project_name,
           u.user_id, coalesce(us.display_name, us.username) AS user_name,
           u.device, u.resource_type, u.quantity, u.unit, u.occurred_at,
           u.source_ref, u.pricing_version
    FROM usage_events u
    LEFT JOIN tenants t ON t.id = u.tenant_id
    LEFT JOIN projects p ON p.id = u.project_id
    LEFT JOIN users us ON us.zitadel_user_id = u.user_id
    WHERE (p_tenant_id IS NULL OR u.tenant_id = p_tenant_id)
      AND (p_resource_type IS NULL OR u.resource_type = p_resource_type)
      AND (p_since IS NULL OR u.occurred_at >= p_since)
      AND (p_q IS NULL
           OR u.resource_type ILIKE '%' || p_q || '%'
           OR u.source_ref ILIKE '%' || p_q || '%'
           OR u.device ILIKE '%' || p_q || '%'
           OR u.user_id ILIKE '%' || p_q || '%'
           OR us.display_name ILIKE '%' || p_q || '%'
           OR us.username ILIKE '%' || p_q || '%'
           OR t.name ILIKE '%' || p_q || '%'
           OR p.name ILIKE '%' || p_q || '%')
  )
  SELECT m.*, count(*) OVER ()
  FROM matching m
  ORDER BY m.occurred_at DESC, m.id DESC
  LIMIT p_limit OFFSET p_offset;
$$;
REVOKE ALL ON FUNCTION admin_usage_events(int, int, uuid, text, text, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION admin_usage_events(int, int, uuid, text, text, timestamptz) TO core_app;
