-- Platform-wide utilisation for the back-office overview's stat cards
-- (app/modules/admin/usage, GET /v1/admin/usage-events/utilization).
--
-- Same SECURITY DEFINER escape as admin_usage_events (0038/0039):
-- usage_events is RLS-protected and the GPU rows belong to no tenant at
-- all, so only a definer function can see them. Aggregates only.
--
-- Both halves read the *latest* row rather than summing history, because
-- both are gauges: a GPU's utilisation is what it read last, and storage
-- is what the most recent snapshot found, not the sum of every snapshot.
--
--   GPU      the latest gpu_utilization and vram_usage reading per
--            device, however old — `gpu_sampled_at` says how old, so a
--            node that stopped reporting shows as stale instead of
--            silently vanishing from the average
--   storage  the latest snapshot per (tenant, project, person, kind),
--            summed across the platform
CREATE OR REPLACE FUNCTION admin_utilization()
RETURNS TABLE(
  gpu_count bigint,
  -- Mean of each GPU's latest reading, in percent. Null with no GPUs.
  gpu_utilization numeric,
  vram_used_bytes numeric,
  -- The newest reading behind those numbers.
  gpu_sampled_at timestamptz,
  storage_dataset_bytes numeric,
  storage_model_bytes numeric
)
LANGUAGE sql SECURITY DEFINER AS $$
  WITH gpu AS (
    SELECT DISTINCT ON (device, resource_type) device, resource_type, quantity, occurred_at
    FROM usage_events
    WHERE resource_type IN ('gpu_utilization', 'vram_usage')
    ORDER BY device, resource_type, occurred_at DESC, id DESC
  ),
  storage AS (
    SELECT DISTINCT ON (tenant_id, project_id, user_id, resource_type) resource_type, quantity
    FROM usage_events
    WHERE resource_type IN ('storage_dataset', 'storage_model')
    ORDER BY tenant_id, project_id, user_id, resource_type, occurred_at DESC, id DESC
  )
  SELECT (SELECT count(DISTINCT device) FROM gpu),
         (SELECT round(avg(quantity), 1) FROM gpu WHERE resource_type = 'gpu_utilization'),
         (SELECT coalesce(sum(quantity), 0) FROM gpu WHERE resource_type = 'vram_usage'),
         (SELECT max(occurred_at) FROM gpu),
         (SELECT coalesce(sum(quantity), 0) FROM storage WHERE resource_type = 'storage_dataset'),
         (SELECT coalesce(sum(quantity), 0) FROM storage WHERE resource_type = 'storage_model');
$$;
REVOKE ALL ON FUNCTION admin_utilization() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION admin_utilization() TO core_app;
