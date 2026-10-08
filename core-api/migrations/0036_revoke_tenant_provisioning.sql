-- The back-office no longer provisions organisations: POST
-- /v1/admin/tenants is gone, and 0028's INSERT grant goes with it, so
-- core_app cannot write a tenant row even through a route someone adds
-- back later without meaning to. Provisioning is back to where it was
-- before 0028 — scripts/zitadel-bootstrap.sh, connected as the postgres
-- superuser. SELECT (0001) and UPDATE (0027, settings) stay.

REVOKE INSERT ON tenants FROM core_app;
