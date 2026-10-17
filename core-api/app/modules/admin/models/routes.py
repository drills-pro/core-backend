"""Every registered model on the platform, across all tenants.

The user-facing registry is tenant-scoped (`/v1/projects/{id}/models`,
app/modules/user/model_registry) and the tenant detail page counts models
per organisation (0030). This is the third view of the same table: every
model side by side, which is the operator's view alone, so it goes through
get_platform_admin and the SECURITY DEFINER function in 0033 rather than
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

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.core.auth import get_platform_admin
from app.core.db import get_pool

router = APIRouter(prefix="/v1/admin/models", tags=["admin"])

# The statuses 0033 derives. Validated here rather than passed through, so
# a typo comes back as a 400 instead of as a convincing empty page.
STATUSES = ("deployed", "deploying", "stopped", "not_deployed")

# projects.task_type (0002) and projects.visibility (0024).
TASK_TYPES = (
    "image_classification",
    "object_detection",
    "tabular_classification",
    "text_classification",
    "legacy_untyped",
)
VISIBILITIES = ("private", "public")


@router.get("", dependencies=[Depends(get_platform_admin)])
async def list_models(
    response: Response,
    q: str | None = Query(default=None, description="matches model, project or organisation"),
    tenant_id: uuid.UUID | None = Query(default=None, description="one organisation only"),
    task_type: str | None = Query(default=None, description="image_classification | ..."),
    visibility: str | None = Query(default=None, description="private | public"),
    status: str | None = Query(default=None, description="deployed | deploying | stopped | not_deployed"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """Every registered model, newest first, one row per model.

    `X-Total-Count` carries the size of the filtered set so a pager costs
    no second request — the same contract as the other admin listings."""
    reject_unknown("status", status, STATUSES)
    reject_unknown("task_type", task_type, TASK_TYPES)
    reject_unknown("visibility", visibility, VISIBILITIES)

    async with (await get_pool()).acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM admin_models($1, $2, $3, $4, $5, $6, $7)",
            limit,
            offset,
            tenant_id,
            task_type,
            visibility,
            status,
            q,
        )
    # count(*) OVER () has no row to ride on when the filter matches
    # nothing, which is the one case the header has to be told about.
    response.headers["X-Total-Count"] = str(rows[0]["total"] if rows else 0)
    # `total` is an implementation detail of the pager, not part of a row.
    return [{k: v for k, v in dict(row).items() if k != "total"} for row in rows]


@router.get("/{version_id}/versions", dependencies=[Depends(get_platform_admin)])
async def list_model_versions(
    response: Response,
    version_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """Every version of the model this version belongs to, newest first.

    Addressed by one of its versions — the id the listing already handed
    out — rather than by (project, name), so the caller has no second key
    to spell and nothing to escape. An id that names no version answers
    with an empty list, the same as a model whose project was deleted."""
    async with (await get_pool()).acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM admin_model_versions($1, $2, $3)", version_id, limit, offset
        )
    response.headers["X-Total-Count"] = str(rows[0]["total"] if rows else 0)
    return [{k: v for k, v in dict(row).items() if k != "total"} for row in rows]


@router.get("/summary", dependencies=[Depends(get_platform_admin)])
async def models_summary():
    """The totals behind the stat strip and the tabs.

    Counted across every tenant rather than over the page on screen, so
    they do not move when a filter does."""
    async with (await get_pool()).acquire() as conn:
        stats = await conn.fetchrow("SELECT * FROM admin_model_stats()")
    return dict(stats)


def reject_unknown(name: str, value: str | None, allowed: tuple[str, ...]) -> None:
    if value is not None and value not in allowed:
        raise HTTPException(
            status_code=400, detail=f"{name} must be one of {', '.join(allowed)}"
        )
