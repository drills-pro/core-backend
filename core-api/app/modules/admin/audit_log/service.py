from app.core.db import get_pool
from app.core.enums import AdminAuditEntity
from app.modules.admin.audit_log.schemas import AdminAuditEntry

TOTAL_COLUMN = "total"  # count(*) OVER () on every row: the pager's, not part of a row
_LIST = (
    "SELECT *, count(*) OVER () AS total FROM admin_audit "
    "WHERE ($1::text IS NULL OR entity_type = $1) AND ($2::text IS NULL OR actor = $2) "
    "ORDER BY created_at DESC, id DESC LIMIT $3 OFFSET $4"
)


async def list_entries(entity_type: AdminAuditEntity | None, actor: str | None, limit: int, offset: int) -> tuple[list[AdminAuditEntry], int]:
    """One page of the trail, newest first, and the size of the filtered set."""
    async with (await get_pool()).acquire() as conn:
        rows = await conn.fetch(_LIST, entity_type, actor, limit, offset)
    # count(*) OVER () has no row to ride on past the last page, so an empty page reports 0
    return [AdminAuditEntry.model_validate(dict(row)) for row in rows], rows[0][TOTAL_COLUMN] if rows else 0
