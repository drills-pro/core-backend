-- migrate:up
-- Succeeded runs of one task whose report has no top-level `classes` (reported before the trainer wrote it), oldest id
-- first after `p_after`, for the backfill (python -m app.modules.user.training.backfill_classes). Across tenants, so
-- SECURITY DEFINER like the other cross-tenant reads; it returns ids only, and the backfill then works per tenant.
CREATE FUNCTION runs_missing_report_classes(p_task text, p_after uuid, p_limit int)
RETURNS TABLE(tenant_id uuid, project_id uuid, id uuid)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT r.tenant_id, r.project_id, r.id
  FROM training_runs r
  WHERE r.task = p_task AND r.status = 'succeeded' AND r.report IS NOT NULL AND NOT r.report ? 'classes'
    AND (p_after IS NULL OR r.id > p_after)
  ORDER BY r.id
  LIMIT p_limit;
$$;
REVOKE ALL ON FUNCTION runs_missing_report_classes(text, uuid, int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION runs_missing_report_classes(text, uuid, int) TO core_app;

-- migrate:down
DROP FUNCTION runs_missing_report_classes(text, uuid, int);
