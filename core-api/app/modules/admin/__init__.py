"""The platform back-office: everything under /v1/admin the operators of
the platform itself reach, across every tenant.

A grouping, not a bounded context of its own — it owns no routes and
exports nothing. Each package below it is a context with its own public
API; importing one is `from app.modules.admin.<m> import thing`.

What separates this group from user/ is the scope of a request: a user/
route is scoped to one tenant by RLS (app.core.tenant_conn.TenantConn),
an admin/ route deliberately is not, so every route here goes through
app.core.auth.get_platform_admin instead of get_tenant_id.
"""
