from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # authz.deps imports this module
    from app.core.authz import AuthzContext


async def record(conn, *, tenant_id: str, project_id, actor: str, action: str, entity_type: str, entity_id):
    """Append one audit_log row (req/03 FR3). Call inside the same
    transaction as the mutation it's recording, using an already
    tenant-scoped connection (see app.core.tenant_conn.TenantConn)."""
    await conn.execute(
        "INSERT INTO audit_log (tenant_id, project_id, actor, action, entity_type, entity_id) "
        "VALUES ($1, $2, $3, $4, $5, $6)",
        tenant_id,
        project_id,
        actor,
        action,
        entity_type,
        str(entity_id),
    )


async def record_for(conn, ctx: "AuthzContext", project_id, action: StrEnum, entity_type: StrEnum, entity_id) -> None:
    """`record` for the caller in `ctx`: the tenant and actor come from it. Each context passes its own AuditAction/EntityType."""
    await record(conn, tenant_id=ctx.tenant_id, project_id=project_id, actor=ctx.actor,
                 action=action, entity_type=entity_type, entity_id=entity_id)
