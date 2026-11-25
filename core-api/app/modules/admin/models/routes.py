"""Every registered model on the platform, across all tenants.

The user-facing registry is tenant-scoped (`/v1/projects/{id}/models`,
app/modules/user/model_registry) and the tenant detail page counts models
per organisation (admin_tenant_projects()). This is the third view of the same table: every
model side by side, which is the operator's view alone, so it goes through
get_platform_admin and the SECURITY DEFINER function admin_models() rather than
an RLS-scoped connection.

One row is one *model* — `model_versions` is one row per version, so the
listing folds them onto (project, model name) and reports the newest, with
`version_count` saying how many are behind it. `GET /{id}/versions` opens
one back up.

Each row is joined onto the project it belongs to (name, task type,
visibility) and onto the newest deployment of the run its newest version
came from, which is what "deployed" or "stopped" means here. All of that
is Postgres — unlike the tenant listing, nothing on this page needs
Zitadel, so nothing here can be slowed down by it.

Two columns the back-office draws have no source yet and are not invented
here: an artifact's size on disk (MLflow holds the artifact; this schema
only points at it) and whether a model is awaiting review (nothing records
a review). They reach the dashboard as absent, and it says so.
"""
import uuid

from fastapi import APIRouter, Depends, Query, Response

from app.core.auth import get_platform_admin
from app.core.enums import TaskType
from app.core.pagination import Page, PageParams, set_total_header
from app.modules.admin.models import service
from app.modules.admin.models.schemas import AdminModel, AdminModelVersion, ModelStats

router = APIRouter(prefix="/v1/admin/models", tags=["admin"])


@router.get("", dependencies=[Depends(get_platform_admin)], response_model=Page[AdminModel])
async def list_models(
    response: Response,
    q: str | None = Query(default=None, description="matches model, project or organisation"),
    tenant_id: uuid.UUID | None = Query(default=None, description="one organisation only"),
    task_type: TaskType | None = Query(default=None),
    visibility: str | None = Query(default=None, description="private | internal"),
    status: str | None = Query(default=None, description="deployed | stopped | not_deployed"),
    page: PageParams = Depends(),
):
    """Every registered model, newest first, one row per model.

    `X-Total-Count` carries the size of the filtered set so a pager costs
    no second request — the same contract as the other admin listings."""
    items, total = await service.list_models(q, tenant_id, task_type, visibility, status, page)
    set_total_header(response, total)
    return items


@router.get("/{version_id}/versions", dependencies=[Depends(get_platform_admin)], response_model=Page[AdminModelVersion])
async def list_model_versions(
    response: Response,
    version_id: uuid.UUID,
    page: PageParams = Depends(),
):
    """Every version of the model this version belongs to, newest first.

    Addressed by one of its versions — the id the listing already handed
    out — rather than by (project, name), so the caller has no second key
    to spell and nothing to escape. An id that names no version answers
    with an empty page, the same as a model whose project was deleted."""
    items, total = await service.list_versions(version_id, page)
    set_total_header(response, total)
    return items


@router.get("/summary", dependencies=[Depends(get_platform_admin)], response_model=ModelStats)
async def models_summary():
    """The totals behind the stat strip and the tabs.

    Counted across every tenant rather than over the page on screen, so
    they do not move when a filter does."""
    return await service.summary()
