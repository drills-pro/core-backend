import uuid
from http import HTTPStatus

from app.core.authz import Visibility
from app.core.db import get_pool
from app.core.enums import TaskType
from app.core.problem import Problem
from app.modules.admin.models.constants import TOTAL_COLUMN, ModelStatus, ProblemCode
from app.modules.admin.models.schemas import AdminModel, AdminModelVersion, ModelStats

_MODELS = "SELECT * FROM admin_models($1, $2, $3, $4, $5, $6, $7)"
_VERSIONS = "SELECT * FROM admin_model_versions($1, $2, $3)"
_STATS = "SELECT * FROM admin_model_stats()"


def _reject_unknown(name: str, value: str | None, allowed: type[ModelStatus] | type[Visibility]) -> None:
    """Validated rather than passed through, so a typo comes back as a 400 instead of as a convincing empty page."""
    if value is not None and value not in allowed:
        raise Problem(HTTPStatus.BAD_REQUEST, ProblemCode.INVALID_FILTER, f"{name} must be one of {', '.join(allowed)}")


def _total(rows) -> int:
    # count(*) OVER () has no row to ride on when the filter matches
    # nothing, which is the one case the header has to be told about.
    return rows[0][TOTAL_COLUMN] if rows else 0


async def list_models(
    q: str | None, tenant_id: uuid.UUID | None, task_type: TaskType | None, visibility: str | None,
    status: str | None, limit: int, offset: int,
) -> tuple[list[AdminModel], int]:
    """One page of models, newest first, and the size of the filtered set."""
    _reject_unknown("status", status, ModelStatus)
    _reject_unknown("visibility", visibility, Visibility)
    async with (await get_pool()).acquire() as conn:
        rows = await conn.fetch(_MODELS, limit, offset, tenant_id, task_type, visibility, status, q)
    return [AdminModel.model_validate(dict(r)) for r in rows], _total(rows)


async def list_versions(version_id: uuid.UUID, limit: int, offset: int) -> tuple[list[AdminModelVersion], int]:
    async with (await get_pool()).acquire() as conn:
        rows = await conn.fetch(_VERSIONS, version_id, limit, offset)
    return [AdminModelVersion.model_validate(dict(r)) for r in rows], _total(rows)


async def summary() -> ModelStats:
    async with (await get_pool()).acquire() as conn:
        return ModelStats.model_validate(dict(await conn.fetchrow(_STATS)))
