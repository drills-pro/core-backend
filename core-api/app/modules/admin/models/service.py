import uuid
from http import HTTPStatus

from app.core.authz import Visibility
from app.core.db import get_pool
from app.core.enums import TaskType
from app.core.pagination import Page, PageParams, build_page, cursor_args, total_of
from app.core.problem import Problem
from app.modules.admin.models.constants import ModelStatus, ProblemCode
from app.modules.admin.models.schemas import AdminModel, AdminModelVersion, ModelStats

_MODELS = "SELECT * FROM admin_models($1, $2, $3, $4, $5, $6, $7, $8)"
_VERSIONS = "SELECT * FROM admin_model_versions($1, $2, $3, $4)"
_STATS = "SELECT * FROM admin_model_stats()"


def _reject_unknown(name: str, value: str | None, allowed: type[ModelStatus] | type[Visibility]) -> None:
    """Validated rather than passed through, so a typo comes back as a 400 instead of as a convincing empty page."""
    if value is not None and value not in allowed:
        raise Problem(HTTPStatus.BAD_REQUEST, ProblemCode.INVALID_FILTER, f"{name} must be one of {', '.join(allowed)}")


async def list_models(
    q: str | None, tenant_id: uuid.UUID | None, task_type: TaskType | None, visibility: str | None,
    status: str | None, page: PageParams,
) -> tuple[Page[AdminModel], int]:
    """One page of models, newest first, and the size of the filtered set."""
    _reject_unknown("status", status, ModelStatus)
    _reject_unknown("visibility", visibility, Visibility)
    async with (await get_pool()).acquire() as conn:
        rows = await conn.fetch(_MODELS, page.page_size + 1, *cursor_args(page.page_token), tenant_id, task_type, visibility, status, q)
    return build_page(rows, page.page_size, AdminModel), total_of(rows)


async def list_versions(version_id: uuid.UUID, page: PageParams) -> tuple[Page[AdminModelVersion], int]:
    async with (await get_pool()).acquire() as conn:
        rows = await conn.fetch(_VERSIONS, version_id, page.page_size + 1, *cursor_args(page.page_token))
    return build_page(rows, page.page_size, AdminModelVersion), total_of(rows)


async def summary() -> ModelStats:
    async with (await get_pool()).acquire() as conn:
        return ModelStats.model_validate(dict(await conn.fetchrow(_STATS)))
