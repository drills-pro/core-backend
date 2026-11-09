-- migrate:up
-- "Try it" calls keep the request and the response of every call; they are a running log for the console, not a record,
-- so they expire. Across tenants, so SECURITY DEFINER like purge_predict_calls: drops what is older than p_days and says how many.
CREATE INDEX deployment_test_calls_created_idx ON deployment_test_calls(created_at);

CREATE FUNCTION purge_test_calls(p_days integer) RETURNS bigint
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  WITH gone AS (DELETE FROM deployment_test_calls WHERE created_at < now() - make_interval(days => p_days) RETURNING 1)
  SELECT count(*) FROM gone;
$$;
REVOKE ALL ON FUNCTION purge_test_calls(integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION purge_test_calls(integer) TO core_app;

-- migrate:down
DROP FUNCTION purge_test_calls(integer);
DROP INDEX deployment_test_calls_created_idx;
