-- migrate:up
ALTER TABLE api_keys ADD COLUMN last_used_at timestamptz;

-- Called after a key authenticates. Writes at most once a minute per key so a busy key is not a write per request.
CREATE FUNCTION api_key_touch(p_key_id uuid) RETURNS void
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  UPDATE api_keys SET last_used_at = now()
  WHERE id = p_key_id AND (last_used_at IS NULL OR last_used_at < now() - interval '1 minute');
$$;
REVOKE ALL ON FUNCTION api_key_touch(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION api_key_touch(uuid) TO core_app;

-- migrate:down
DROP FUNCTION api_key_touch(uuid);
ALTER TABLE api_keys DROP COLUMN last_used_at;
