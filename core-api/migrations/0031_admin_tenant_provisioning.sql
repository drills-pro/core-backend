-- migrate:up
-- The back-office provisions organisations now (POST /v1/admin/tenants),
-- which until this point only scripts/zitadel-bootstrap.sh did, connected
-- as the postgres superuser.
--
-- 0001 granted core_app SELECT on tenants and 0030 added UPDATE (settings).
-- INSERT is the last thing provisioning needs: the row it writes is the
-- mapping to a Zitadel org, and everything else about a tenant still
-- belongs to whoever owns that system (Zitadel: name and state, billing:
-- plan tier). Deliberately no DELETE — deleting a tenant would orphan
-- every project, dataset and model that references it.
GRANT INSERT ON tenants TO core_app;

-- migrate:down
REVOKE INSERT ON tenants FROM core_app;
