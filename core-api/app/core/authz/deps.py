"""The FastAPI side of authorization: `require(action)` as a route dependency.

Flow per request: verify the token and resolve the tenant (core.tenancy: 401, or 404 for a tenant
the caller is not in), then one `project_access` call decides the rest.

    suspended tenant                            -> 403 tenant_suspended
    project not in this tenant / unbound private -> 404, the same body as a missing resource
    visible but action not allowed              -> 403 forbidden, naming the action
"""
import uuid
from dataclasses import dataclass
from enum import StrEnum

from fastapi import Depends, Request

from app.core import audit
from app.core.authz.policy import (
    MANAGER_ROLES,
    PROJECT_ACTIONS,
    TENANT_ACTIONS,
    Action,
    DefaultAccess,
    Role,
    TenantRole,
    TenantStatus,
    Visibility,
    allowed_actions,
)
from app.core.problem import Problem, not_found
from app.core.settings import settings
from app.core.tenancy import get_actor, get_tenant_id
from app.core.tenant_conn import TenantConn


class AuditAction(StrEnum):
    IMPLICIT_ACCESS = "implicit_access"  # followed by ".<action>": an owner/admin acted without a binding


READ_ACTIONS = frozenset(a for a in Action if a.value.endswith(".read"))


@dataclass(frozen=True, slots=True)
class AuthzContext:
    tenant_id: str
    actor: str
    tenant_role: TenantRole
    project_id: uuid.UUID | None  # None for tenant-level actions
    actions: frozenset[Action]

    async def accessible_project_ids(self, conn) -> list[uuid.UUID]:
        """For list endpoints: `WHERE project_id = ANY($n)` keeps rows the caller cannot see out."""
        return await conn.fetchval("SELECT array_agg(i) FROM accessible_project_ids($1, $2) i", self.actor, self.tenant_id) or []


def _parse_uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise not_found() from None


def require(action: Action):
    """Route dependency that enforces `action` and returns an AuthzContext.

    Tenant-level actions (Action in TENANT_ACTIONS) need no project. Any other action acts on the
    project in the path (`project_id`); a route keyed only by a child id must be nested under it.
    """
    project_scoped = action in PROJECT_ACTIONS

    async def dependency(
        request: Request,
        tenant_id: str = Depends(get_tenant_id),
        actor: str = Depends(get_actor),
    ) -> AuthzContext:
        async with TenantConn(tenant_id) as conn:
            project_id = _parse_uuid(request.path_params["project_id"]) if project_scoped else None
            row = await conn.fetchrow("SELECT * FROM project_access($1, $2, $3)", actor, tenant_id, project_id)
            if row is None and project_scoped and settings.auth.dev_headers:
                if await conn.fetchval("SELECT 1 FROM projects WHERE id = $1", project_id) is None:
                    raise not_found()

        if row is None:
            # Only a dev-header caller can reach here with a tenant it is not a member of (a token
            # caller was already 404ed by get_tenant_id). dev_headers is refused in prod.
            if not settings.auth.dev_headers:
                raise not_found()
            return AuthzContext(tenant_id, actor, TenantRole.OWNER, project_id, frozenset(Action))
        if row["tenant_status"] == TenantStatus.SUSPENDED:
            raise Problem(403, "tenant_suspended", "this tenant is suspended")

        tenant_role = TenantRole(row["tenant_role"])
        visibility = Visibility(row["visibility"]) if row["visibility"] else None
        if project_scoped and visibility is None:
            raise not_found()  # no such project in this tenant
        default_access = DefaultAccess(row["default_access"]) if visibility else None
        bound = [Role(r) for r in row["roles"] or ()]
        actions = allowed_actions(tenant_role, visibility, default_access, bound)
        if project_scoped and not actions & PROJECT_ACTIONS:
            raise not_found()  # private and unbound: do not reveal it exists
        if action not in actions:
            raise Problem(403, "forbidden", f"missing action {action}", action=action.value)
        if project_scoped and tenant_role in MANAGER_ROLES and action not in READ_ACTIONS:
            if action not in allowed_actions(TenantRole.MEMBER, visibility, default_access, bound):
                async with TenantConn(tenant_id) as conn:  # the grant came from the tenant role alone
                    await audit.record(
                        conn, tenant_id=tenant_id, project_id=project_id, actor=actor,
                        action=f"{AuditAction.IMPLICIT_ACCESS}.{action}", entity_type="project", entity_id=project_id,
                    )
        return AuthzContext(tenant_id, actor, tenant_role, project_id, actions)

    dependency.authz_action = action  # read by tests/core/authz/test_matrix.py
    return dependency


def public() -> None:
    """Mark a route that needs no tenant or project action (health, gallery, /me): a no-op dependency
    the matrix test looks for, so "unprotected" is always a decision someone wrote down."""
