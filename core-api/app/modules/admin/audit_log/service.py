from app.core.db import get_pool
from app.core.enums import AdminAuditEntity
from app.core.pagination import TOTAL_COLUMN, Page, PageParams, SortDir, build_page, keyset_clause, order_by, total_of
from app.modules.admin.audit_log.schemas import AdminAuditEntry

# The count is taken over the filtered set in the inner query; the cursor then cuts the page out of it.
_LIST = (
    f"SELECT * FROM (SELECT *, count(*) OVER () AS {TOTAL_COLUMN} FROM admin_audit "
    "WHERE ($1::text IS NULL OR entity_type = $1) AND ($2::text IS NULL OR actor = $2)) AS matching WHERE true"
)


async def list_entries(entity_type: AdminAuditEntity | None, actor: str | None, page: PageParams) -> tuple[Page[AdminAuditEntry], int]:
    """One page of the trail, newest first, and the size of the filtered set."""
    cond, args = keyset_clause(page.page_token, SortDir.DESC, first_param=3)
    async with (await get_pool()).acquire() as conn:
        rows = await conn.fetch(f"{_LIST} {cond} ORDER BY {order_by(SortDir.DESC)} LIMIT {page.page_size + 1}", entity_type, actor, *args)
    return build_page(rows, page.page_size, AdminAuditEntry), total_of(rows)
