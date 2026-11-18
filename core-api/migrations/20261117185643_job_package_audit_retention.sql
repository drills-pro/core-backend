-- migrate:up
-- Keeps the newest p_keep rows of each package name in job_package_audit and deletes the rest: the packages log, not the
-- registry (job_packages rows stay). Across names, so SECURITY DEFINER like the other purges: the table is granted only
-- SELECT and INSERT to core_app.
CREATE FUNCTION purge_job_package_audit(p_keep integer) RETURNS bigint
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  WITH ranked AS (
    SELECT id, row_number() OVER (PARTITION BY name ORDER BY created_at DESC, id DESC) AS rank_in_name
    FROM job_package_audit
  ), gone AS (
    -- migration-lint: allow-destructive (retention: an audit row past the newest p_keep of its package name)
    DELETE FROM job_package_audit a
    USING ranked r
    WHERE a.id = r.id AND r.rank_in_name > p_keep
    RETURNING 1
  )
  SELECT count(*) FROM gone;
$$;
REVOKE ALL ON FUNCTION purge_job_package_audit(integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION purge_job_package_audit(integer) TO core_app;

-- migrate:down
DROP FUNCTION purge_job_package_audit(integer);
