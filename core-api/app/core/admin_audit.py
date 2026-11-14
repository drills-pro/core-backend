"""Platform-level audit trail of /v1/admin writes (admin_audit): append-only, no tenant, so no RLS. Read it through
GET /v1/admin/audit-log (app/modules/admin/audit_log)."""
from app.core.enums import AdminAuditAction, AdminAuditEntity

_INSERT = "INSERT INTO admin_audit (actor, action, entity_type, entity_id, detail) VALUES ($1, $2, $3, $4, $5)"


async def record(
    conn, actor: str, action: AdminAuditAction, entity_type: AdminAuditEntity, entity_id, detail: dict | None = None
) -> None:
    """Append one admin_audit row. Call it in the transaction of the write it records, so both land or neither does."""
    await conn.execute(_INSERT, actor, action, entity_type, str(entity_id), detail or {})
