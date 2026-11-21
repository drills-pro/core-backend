"""The tenant status machine, written once. `pending_delete` is entered only by the owner's delete and left only by
the owner's restore; the operator's active/suspended switch leaves a pending_delete tenant as it is."""
import uuid
from http import HTTPStatus

from app.core.authz import TenantStatus
from app.core.authz.deps import TenantProblem
from app.core.problem import Problem
from app.modules.user.tenants.constants import ProblemCode
from app.modules.user.tenants.schemas import TenantStatusChange

# What an operator may set; pending_delete is the owner's flow, not a switch.
ADMIN_SETTABLE_STATUSES = (TenantStatus.ACTIVE, TenantStatus.SUSPENDED)

_MARK_PENDING_DELETE = "UPDATE tenants SET status = $2, deleted_at = now() WHERE id = $1"
_RESTORE = "UPDATE tenants SET status = $2, deleted_at = NULL WHERE id = $1 AND status = $3 RETURNING id"
# One statement, the row locked: the owner's :delete cannot slip in between the check and the write.
_ADMIN_SET = (
    "WITH cur AS (SELECT id, status, coalesce(zitadel_org_id, '') AS org_id FROM tenants WHERE id = $1 FOR UPDATE), "
    "moved AS (UPDATE tenants t SET status = $2 FROM cur WHERE t.id = cur.id AND cur.status <> $3) "
    "SELECT status AS previous, org_id FROM cur"
)


async def mark_pending_delete(conn, tenant_id: uuid.UUID) -> None:
    """The owner's soft delete, from any status. Restorable until the purge."""
    await conn.execute(_MARK_PENDING_DELETE, tenant_id, TenantStatus.PENDING_DELETE)


async def restore(conn, tenant_id: uuid.UUID) -> None:
    """pending_delete -> active. 409 not_pending_delete for a tenant in any other status."""
    restored = await conn.fetchval(_RESTORE, tenant_id, TenantStatus.ACTIVE, TenantStatus.PENDING_DELETE)
    if restored is None:
        raise Problem(HTTPStatus.CONFLICT, ProblemCode.NOT_PENDING_DELETE, "this tenant is not pending deletion")


async def set_status(conn, tenant_id: uuid.UUID, status: TenantStatus) -> TenantStatusChange | None:
    """The operator's switch to `status` (one of ADMIN_SETTABLE_STATUSES). The status it had and its Zitadel org id
    ('' when none), or None when there is no such tenant. 409 tenant_pending_delete, nothing written, when the
    tenant is pending_delete: only its owner can restore it."""
    row = await conn.fetchrow(_ADMIN_SET, tenant_id, status, TenantStatus.PENDING_DELETE)
    if row is None:
        return None
    change = TenantStatusChange.model_validate(dict(row))
    if change.previous == TenantStatus.PENDING_DELETE:
        raise Problem(HTTPStatus.CONFLICT, TenantProblem.PENDING_DELETE, "tenant is pending deletion; only its owner can restore it")
    return change
