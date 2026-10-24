-- migrate:up
-- Background sweeps (docs/datasets-plan.md step 8) find work across tenants before setting app.tenant_id.
-- Versions stay immutable for core_app, so the final purge of a deleted dataset is a function, not a DELETE grant.

CREATE FUNCTION expired_import_tenant_ids(p_now timestamptz) RETURNS SETOF uuid
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT DISTINCT tenant_id FROM dataset_imports
  WHERE status IN ('awaiting_upload', 'awaiting_review', 'committing') AND expires_at < p_now;
$$;

CREATE FUNCTION purgeable_datasets(p_cutoff timestamptz) RETURNS TABLE(tenant_id uuid, id uuid)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT d.tenant_id, d.id FROM datasets d WHERE d.deleted_at < p_cutoff;
$$;

-- Removes the rows of a dataset whose retention window has passed; false when it was restored meanwhile.
CREATE FUNCTION purge_dataset_rows(p_dataset_id uuid, p_cutoff timestamptz) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
  -- SECURITY DEFINER bypasses RLS, so the caller's tenant is checked here: a bug elsewhere in core_app
  -- cannot purge another tenant's dataset.
  PERFORM 1 FROM datasets
    WHERE id = p_dataset_id AND deleted_at < p_cutoff AND tenant_id = current_setting('app.tenant_id', true)::uuid
    FOR UPDATE;
  IF NOT FOUND THEN
    RETURN false;
  END IF;
  UPDATE datasets SET published_version_id = NULL WHERE id = p_dataset_id;
  UPDATE dataset_imports SET base_version_id = NULL WHERE dataset_id = p_dataset_id;
  UPDATE projects SET default_test_dataset_id = NULL WHERE default_test_dataset_id = p_dataset_id;
  UPDATE training_runs SET dataset_id = NULL WHERE dataset_id = p_dataset_id;
  UPDATE training_runs SET test_dataset_id = NULL WHERE test_dataset_id = p_dataset_id;
  DELETE FROM dataset_versions WHERE dataset_id = p_dataset_id;
  DELETE FROM dataset_imports WHERE dataset_id = p_dataset_id;
  DELETE FROM datasets WHERE id = p_dataset_id;
  RETURN true;
END;
$$;

REVOKE ALL ON FUNCTION expired_import_tenant_ids(timestamptz) FROM PUBLIC;
REVOKE ALL ON FUNCTION purgeable_datasets(timestamptz) FROM PUBLIC;
REVOKE ALL ON FUNCTION purge_dataset_rows(uuid, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION expired_import_tenant_ids(timestamptz) TO core_app;
GRANT EXECUTE ON FUNCTION purgeable_datasets(timestamptz) TO core_app;
GRANT EXECUTE ON FUNCTION purge_dataset_rows(uuid, timestamptz) TO core_app;

-- migrate:down
DROP FUNCTION purge_dataset_rows(uuid, timestamptz);
DROP FUNCTION purgeable_datasets(timestamptz);
DROP FUNCTION expired_import_tenant_ids(timestamptz);
