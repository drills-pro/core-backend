-- migrate:up
-- Opening an accepted invitation again (a second click on the mailed link, or the SPA sending the token twice)
-- answered 404, so the person who had just joined saw "this invitation cannot be used". Now the person who
-- accepted it, signed in with the invited address and still a member, gets the workspace back with joined = false,
-- so the SPA opens it and the service writes no second audit row. Anyone else still gets nothing (one 404).
-- The return type changes, so the function is dropped and created again rather than replaced.
DROP FUNCTION accept_tenant_invitation(text, text, text);
CREATE FUNCTION accept_tenant_invitation(p_token_hash text, p_email text, p_sub text)
RETURNS TABLE(tenant_id uuid, role text, joined boolean)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  WITH inv AS (
    UPDATE tenant_invitations SET accepted_at = now()
    WHERE token_hash = p_token_hash AND accepted_at IS NULL AND expires_at > now()
      AND lower(email) = lower(p_email)
    RETURNING tenant_invitations.tenant_id AS tid, tenant_invitations.role AS r
  ), mem AS (
    INSERT INTO tenant_members (tenant_id, user_sub, role) SELECT tid, p_sub, r FROM inv
    ON CONFLICT DO NOTHING
  ), again AS (
    -- Reads the statement's snapshot, from before inv's UPDATE, so it never matches the row inv just claimed.
    SELECT m.tenant_id AS tid, m.role AS r FROM tenant_invitations i
    JOIN tenant_members m ON m.tenant_id = i.tenant_id AND m.user_sub = p_sub
    WHERE i.token_hash = p_token_hash AND i.accepted_at IS NOT NULL AND lower(i.email) = lower(p_email)
  )
  SELECT tid, r, true FROM inv
  UNION ALL
  SELECT tid, r, false FROM again;
$$;
REVOKE ALL ON FUNCTION accept_tenant_invitation(text, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION accept_tenant_invitation(text, text, text) TO core_app;

-- migrate:down
DROP FUNCTION accept_tenant_invitation(text, text, text);
CREATE FUNCTION accept_tenant_invitation(p_token_hash text, p_email text, p_sub text)
RETURNS TABLE(tenant_id uuid, role text)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  WITH inv AS (
    UPDATE tenant_invitations SET accepted_at = now()
    WHERE token_hash = p_token_hash AND accepted_at IS NULL AND expires_at > now()
      AND lower(email) = lower(p_email)
    RETURNING tenant_invitations.tenant_id AS tid, tenant_invitations.role AS r
  ), mem AS (
    INSERT INTO tenant_members (tenant_id, user_sub, role) SELECT tid, p_sub, r FROM inv
    ON CONFLICT DO NOTHING
  )
  SELECT tid, r FROM inv;
$$;
REVOKE ALL ON FUNCTION accept_tenant_invitation(text, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION accept_tenant_invitation(text, text, text) TO core_app;
