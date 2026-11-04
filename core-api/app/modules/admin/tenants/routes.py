"""Cross-tenant listing of every tenant on the platform (req/01 FR1 is
about isolating tenants from *each other* — the operator's back-office is
the one audience allowed to see them side by side). Where each field comes
from: service.py.

`GET /summary` alongside it is the same three systems asked for platform
totals instead of rows — the stat strip above the listing.
"""
import json

from fastapi import APIRouter, Body, Depends, Query, Response

from app.core.auth import get_platform_admin
from app.modules.admin.tenants import service
from app.modules.admin.tenants.schemas import CreatedTenant, TenantListItem, TenantsSummary

router = APIRouter(prefix="/v1/admin/tenants", tags=["admin"])


@router.get("", dependencies=[Depends(get_platform_admin)], response_model=list[TenantListItem])
async def list_tenants(
    response: Response,
    q: str | None = Query(default=None, description="case-insensitive match on tenant name"),
    plan_tier: str | None = Query(default=None, description="free | pro | enterprise"),
    status: str | None = Query(default=None, description="active | suspended | unknown"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """Every tenant, newest last.

    `X-Total-Count` carries the size of the filtered set and
    `X-Status-Counts` its active/suspended/unknown breakdown, so a pager
    and status tabs cost no extra requests."""
    page = await service.list_page(q, plan_tier, status, limit, offset)
    response.headers["X-Status-Counts"] = json.dumps(page.status_counts)  # StrEnum keys dump as their values
    response.headers["X-Total-Count"] = str(page.total)
    return page.rows


@router.post("", status_code=201, response_model=CreatedTenant)
async def create_tenant(
    name: str = Body(..., embed=True),
    plan_tier: str = Body(default="free", embed=True),
    zitadel_org_id: str | None = Body(default=None, embed=True),
    _actor: str = Depends(get_platform_admin),
):
    """Provision an organisation.

    Three steps, in the order scripts/zitadel-bootstrap.sh does them by
    hand: the Zitadel org, the tenant row that maps to it, and the Default
    team every tenant is assumed to have (FR1 — project creation
    falls back to it, so a tenant without one cannot hold a project).

    `zitadel_org_id` adopts an org that already exists rather than making
    a new one, which is the case the bootstrap script covers: the org came
    first and only the platform-side mapping is missing.
    """
    return await service.create_tenant(name, plan_tier, zitadel_org_id)


@router.get("/summary", dependencies=[Depends(get_platform_admin)], response_model=TenantsSummary)
async def tenants_summary():
    """The platform-wide totals behind the back-office stat cards.

    Deliberately unfiltered: the cards sit above the listing's tabs and
    filters and describe the platform, not the page under them.
    `status_counts` is the same breakdown the listing sends as `X-Status-Counts`.
    """
    return await service.summary()
