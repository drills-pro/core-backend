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
from http import HTTPStatus

from fastapi import Depends, Request

from app.core import audit
from app.core.authz.policy import (
    MANAGER_ROLES,
    PROJECT_ACTIONS,
    Action,
    DefaultAccess,
    Role,
    TenantRole,
    TenantStatus,
    Visibility,
    allowed_actions,
)
from app.core.problem import Problem, ProblemCode, not_found
from app.core.settings import settings
from app.core.tenancy import get_actor, get_tenant_id
from app.core.tenant_conn import TenantConn


class TenantProblem(StrEnum):
    SUSPENDED = "tenant_suspended"
    PENDING_DELETE = "tenant_pending_delete"


# The audit action of an owner/admin acting without a binding: "implicit_access.<action>", one member per project action.
ImplicitAccess = StrEnum("ImplicitAccess", {a.name: f"implicit_access.{a.value}" for a in PROJECT_ACTIONS})


READ_ACTIONS = frozenset(a for a in Action if a.value.endswith(".read"))


@dataclass(frozen=True, slots=True)
class AuthzContext:
    tenant_id: str
    actor: str
    tenant_role: TenantRole
    project_id: uuid.UUID | None  # None for tenant-level actions
    actions: frozenset[Action]

    def ensure_tenant(self, tenant_id: uuid.UUID) -> None:
        """The path names the tenant, the X-Tenant-Id header (checked for membership) selects it: they must agree."""
        if str(tenant_id) != self.tenant_id:
            raise not_found()

    async def accessible_project_ids(self, conn) -> list[uuid.UUID]:
        """For list endpoints: `WHERE project_id = ANY($n)` keeps rows the caller cannot see out."""
        return await conn.fetchval("SELECT array_agg(i) FROM accessible_project_ids($1, $2) i", self.actor, self.tenant_id) or []


_PROJECT_EXISTS = "SELECT 1 FROM projects WHERE id = $1"  # RLS: only a project of the connection's tenant is seen


def _parse_uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise not_found() from None


async def authorize(
    action: Action, tenant_id: str, actor: str, project_id: uuid.UUID | None, *, allow_pending_delete: bool = False,
) -> AuthzContext:
    """Decide `action` for `actor` in `tenant_id` (on `project_id` for a project action) and return the AuthzContext, or raise: the
    whole of `require`, for a caller that finds the project somewhere other than the path (POST /v1/predict reads X-Project-Id)."""
    project_scoped = action in PROJECT_ACTIONS
    async with TenantConn(tenant_id) as conn:
        row = await conn.fetchrow("SELECT * FROM project_access($1, $2, $3)", actor, tenant_id, project_id)
        if row is None and project_scoped and settings.auth.dev_headers:
            if await conn.fetchval(_PROJECT_EXISTS, project_id) is None:
                raise not_found()
        ctx, implicit = _decide(action, row, tenant_id, actor, project_id, allow_pending_delete)
        if implicit:  # the grant came from the tenant role alone: audited on the connection that decided it
            await audit.record(
                conn, tenant_id=tenant_id, project_id=project_id, actor=actor,
                action=ImplicitAccess[action.name], entity_type="project", entity_id=project_id,
            )
    return ctx


def _decide(
    action: Action, row, tenant_id: str, actor: str, project_id: uuid.UUID | None, allow_pending_delete: bool,
) -> tuple[AuthzContext, bool]:
    """The decision on one project_access row: the context, or a raise. The flag says an owner or admin got a write the
    default and bound roles alone would not give (to be audited)."""
    project_scoped = action in PROJECT_ACTIONS
    if row is None:
        # Only a dev-header caller can reach here with a tenant it is not a member of (a token
        # caller was already 404ed by get_tenant_id). dev_headers is refused in prod.
        if not settings.auth.dev_headers:
            raise not_found()
        return AuthzContext(tenant_id, actor, TenantRole.OWNER, project_id, frozenset(Action)), False
    if row["tenant_status"] == TenantStatus.SUSPENDED:
        raise Problem(403, TenantProblem.SUSPENDED, "this tenant is suspended")
    if row["tenant_status"] == TenantStatus.PENDING_DELETE and not allow_pending_delete:
        raise Problem(403, TenantProblem.PENDING_DELETE, "this tenant is pending deletion")

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
        raise Problem(HTTPStatus.FORBIDDEN, ProblemCode.FORBIDDEN, f"missing action {action}", action=action.value)
    implicit = (
        project_scoped and tenant_role in MANAGER_ROLES and action not in READ_ACTIONS
        and action not in allowed_actions(TenantRole.MEMBER, visibility, default_access, bound)
    )
    return AuthzContext(tenant_id, actor, tenant_role, project_id, actions), implicit


def require(action: Action, *, allow_pending_delete: bool = False):
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
        project_id = _parse_uuid(request.path_params["project_id"]) if project_scoped else None
        return await authorize(action, tenant_id, actor, project_id, allow_pending_delete=allow_pending_delete)

    dependency.authz_action = action  # read by tests/core/authz/test_matrix.py
    return dependency


def public() -> None:
    """Mark a route that needs no tenant or project action (health, gallery, /me): a no-op dependency
    the matrix test looks for, so "unprotected" is always a decision someone wrote down."""
