"""GET /v1/admin/audit-log: the append-only trail of /v1/admin writes (the table is written by app/core/admin_audit.py).

Not to be confused with /v1/admin/activity, which reads the tenants' own audit_log."""
from fastapi import APIRouter, Depends, Query, Response

from app.core.auth import get_platform_admin
from app.core.enums import AdminAuditEntity
from app.core.pagination import Page, PageParams, set_total_header
from app.modules.admin.audit_log import service
from app.modules.admin.audit_log.schemas import AdminAuditEntry

router = APIRouter(prefix="/v1/admin/audit-log", tags=["admin"])


@router.get("", dependencies=[Depends(get_platform_admin)], response_model=Page[AdminAuditEntry])
async def list_audit_log(
    response: Response,
    entity_type: AdminAuditEntity | None = Query(default=None),
    actor: str | None = Query(default=None, description="the admin's subject, exactly"),
    page: PageParams = Depends(),
):
    """Every admin write, newest first. `X-Total-Count` carries the size of the filtered set, as in `GET /v1/admin/activity`."""
    items, total = await service.list_entries(entity_type, actor, page)
    set_total_header(response, total)
    return items
