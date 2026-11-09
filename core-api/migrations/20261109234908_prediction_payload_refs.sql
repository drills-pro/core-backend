-- migrate:up
-- A payload over predict.inline_max_bytes lives in the staging bucket (planning/24): the row holds the object key in
-- input_ref / output_ref and the matching jsonb stays NULL. At most one of the pair is set (both are NULL before the output
-- exists and after the retention purge).
ALTER TABLE predictions
  ADD COLUMN input_ref  text,
  ADD COLUMN output_ref text,
  ADD CONSTRAINT predictions_input_inline_or_ref_check  CHECK (input IS NULL OR input_ref IS NULL),
  ADD CONSTRAINT predictions_output_inline_or_ref_check CHECK (output IS NULL OR output_ref IS NULL);

-- What the purge must delete from the bucket before it clears the rows: the expired rows that hold an object.
CREATE FUNCTION expiring_prediction_refs() RETURNS TABLE (id uuid, tenant_id uuid, project_id uuid)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT p.id, p.tenant_id, p.project_id FROM predictions p
  WHERE p.expires_at < now() AND p.purged_at IS NULL AND (p.input_ref IS NOT NULL OR p.output_ref IS NOT NULL);
$$;
REVOKE ALL ON FUNCTION expiring_prediction_refs() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION expiring_prediction_refs() TO core_app;

-- The purge touches a row that holds an object only once the caller says (`cleaned`) the object is gone, so a failed delete is
-- retried on the next run and never leaves an orphan behind a cleared reference.
DROP FUNCTION purge_predictions();
CREATE FUNCTION purge_predictions(cleaned uuid[] DEFAULT '{}') RETURNS bigint
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  WITH dropped AS (
         DELETE FROM predictions
         WHERE mode = 'sync' AND expires_at < now() AND ((input_ref IS NULL AND output_ref IS NULL) OR id = ANY(cleaned)) RETURNING 1),
       scrubbed AS (
         UPDATE predictions SET input = NULL, output = NULL, input_ref = NULL, output_ref = NULL, purged_at = now()
         WHERE mode = 'background' AND expires_at < now() AND purged_at IS NULL AND ((input_ref IS NULL AND output_ref IS NULL) OR id = ANY(cleaned))
         RETURNING id),
       events AS (DELETE FROM prediction_events WHERE prediction_id IN (SELECT id FROM scrubbed) RETURNING 1)
  SELECT (SELECT count(*) FROM dropped) + (SELECT count(*) FROM scrubbed);
$$;
REVOKE ALL ON FUNCTION purge_predictions(uuid[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION purge_predictions(uuid[]) TO core_app;

-- migrate:down
DROP FUNCTION purge_predictions(uuid[]);
CREATE FUNCTION purge_predictions() RETURNS bigint
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  WITH dropped AS (DELETE FROM predictions WHERE mode = 'sync' AND expires_at < now() RETURNING 1),
       scrubbed AS (
         UPDATE predictions SET input = NULL, output = NULL, purged_at = now()
         WHERE mode = 'background' AND expires_at < now() AND purged_at IS NULL RETURNING id),
       events AS (DELETE FROM prediction_events WHERE prediction_id IN (SELECT id FROM scrubbed) RETURNING 1)
  SELECT (SELECT count(*) FROM dropped) + (SELECT count(*) FROM scrubbed);
$$;
REVOKE ALL ON FUNCTION purge_predictions() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION purge_predictions() TO core_app;
DROP FUNCTION expiring_prediction_refs();
ALTER TABLE predictions DROP COLUMN output_ref, DROP COLUMN input_ref;
