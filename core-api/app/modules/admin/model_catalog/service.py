import logging
import uuid
from http import HTTPStatus

import asyncpg

from app.core import admin_audit
from app.core.db import get_pool
from app.core.enums import AdminAuditAction, AdminAuditEntity, CatalogStatus
from app.core.problem import Problem, not_found
from app.core.settings import settings
from app.integrations import hub
from app.modules.admin.model_catalog.constants import STATUS_AUDIT_ACTION, ProblemCode
from app.modules.admin.model_catalog.schemas import AdminCatalogModel, CatalogModelCreate, CatalogModelUpdate

logger = logging.getLogger(__name__)

_SELECT = "SELECT * FROM model_catalog"
_LIST = f"{_SELECT} WHERE ($1::text IS NULL OR task = $1) AND ($2::text IS NULL OR status = $2) ORDER BY task, display_name, created_at"
_INSERT = (
    "INSERT INTO model_catalog (source, repo_id, revision, display_name, task, license, params, size_bytes, status, created_by, updated_by) "
    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $10) RETURNING *"
)
_UPDATE = (
    "UPDATE model_catalog SET display_name = COALESCE($2, display_name), task = COALESCE($3, task), license = COALESCE($4, license), "
    "params = COALESCE($5, params), size_bytes = COALESCE($6, size_bytes), updated_by = $7, updated_at = now() WHERE id = $1 RETURNING *"
)
_SET_STATUS = "UPDATE model_catalog SET status = $2, updated_by = $3, updated_at = now() WHERE id = $1 RETURNING *"
# A preset pins a base model by repo and commit; a catalog entry one still pins cannot go.
_DELETE_UNUSED = (
    "DELETE FROM model_catalog c WHERE c.id = $1 AND NOT EXISTS "
    "(SELECT 1 FROM task_presets p WHERE p.base_model_repo = c.repo_id AND p.base_model_revision = c.revision) RETURNING c.id"
)
_EXISTS = "SELECT EXISTS (SELECT 1 FROM model_catalog WHERE id = $1)"
_TASK_EXISTS = "SELECT EXISTS (SELECT 1 FROM task_types WHERE slug = $1)"


def _entry(row) -> AdminCatalogModel:
    return AdminCatalogModel.model_validate(dict(row))


async def _require_task(conn, task: str) -> None:
    if not await conn.fetchval(_TASK_EXISTS, task):
        raise Problem(HTTPStatus.UNPROCESSABLE_ENTITY, ProblemCode.TASK_NOT_SUPPORTED, f"task type '{task}' is not supported")


def _require_license(license_id: str) -> None:
    if license_id.lower() not in settings.training.model_license_allowlist:
        raise Problem(HTTPStatus.UNPROCESSABLE_ENTITY, ProblemCode.LICENSE_NOT_ALLOWED, f"licence {license_id} is not in the allowlist")


async def _admit(body: CatalogModelCreate) -> CatalogModelCreate:
    """Without a Hub (local, tests) the admin's licence is checked against the allowlist; with one, the Hub's facts are the
    policy (docs 17, admission) and replace what the admin typed."""
    if not settings.training.hub_url:
        _require_license(body.license)
        return body
    try:
        facts = await hub.fetch(body.repo_id, body.revision)
    except hub.HubUnavailable as e:
        raise Problem(HTTPStatus.BAD_GATEWAY, ProblemCode.HUB_UNAVAILABLE, str(e))
    if reasons := hub.refusals(facts):
        raise Problem(HTTPStatus.UNPROCESSABLE_ENTITY, ProblemCode.MODEL_NOT_ADMITTED, f"{body.repo_id}: " + "; ".join(reasons))
    return body.model_copy(update={"license": facts.license, "size_bytes": facts.total_bytes})


async def list_models(task: str | None, status: CatalogStatus | None) -> list[AdminCatalogModel]:
    async with (await get_pool()).acquire() as conn:
        return [_entry(r) for r in await conn.fetch(_LIST, task, status)]


async def get_model(model_id: uuid.UUID) -> AdminCatalogModel:
    async with (await get_pool()).acquire() as conn:
        row = await conn.fetchrow(f"{_SELECT} WHERE id = $1", model_id)
    if row is None:
        raise not_found()
    return _entry(row)


async def create(body: CatalogModelCreate, actor: str) -> AdminCatalogModel:
    async with (await get_pool()).acquire() as conn:
        await _require_task(conn, body.task)
        body = await _admit(body)
        try:
            async with conn.transaction():
                row = await conn.fetchrow(
                    _INSERT, body.source, body.repo_id, body.revision, body.display_name, body.task, body.license,
                    body.params, body.size_bytes, body.status, actor)
                await admin_audit.record(conn, actor, AdminAuditAction.MODEL_CATALOG_CREATE, AdminAuditEntity.MODEL_CATALOG, row["id"], body.model_dump(mode="json"))
        except asyncpg.UniqueViolationError:
            raise Problem(HTTPStatus.CONFLICT, ProblemCode.ALREADY_IN_CATALOG, f"{body.repo_id}@{body.revision} is already listed for {body.task}")
    logger.info("catalog model %s created by %s", row["id"], actor)
    return _entry(row)


async def update(model_id: uuid.UUID, body: CatalogModelUpdate, actor: str) -> AdminCatalogModel:
    if body.license is not None:
        _require_license(body.license)
    async with (await get_pool()).acquire() as conn:
        if body.task is not None:
            await _require_task(conn, body.task)
        try:
            async with conn.transaction():
                row = await conn.fetchrow(_UPDATE, model_id, body.display_name, body.task, body.license, body.params, body.size_bytes, actor)
                if row is not None:
                    await admin_audit.record(
                        conn, actor, AdminAuditAction.MODEL_CATALOG_UPDATE, AdminAuditEntity.MODEL_CATALOG, model_id,
                        body.model_dump(mode="json", exclude_unset=True))
        except asyncpg.UniqueViolationError:
            raise Problem(HTTPStatus.CONFLICT, ProblemCode.ALREADY_IN_CATALOG, "that model is already listed for that task")
    if row is None:
        raise not_found()
    logger.info("catalog model %s updated by %s", model_id, actor)
    return _entry(row)


async def set_status(model_id: uuid.UUID, status: CatalogStatus, actor: str) -> AdminCatalogModel:
    async with (await get_pool()).acquire() as conn, conn.transaction():
        row = await conn.fetchrow(_SET_STATUS, model_id, status, actor)
        if row is not None:
            await admin_audit.record(conn, actor, STATUS_AUDIT_ACTION[status], AdminAuditEntity.MODEL_CATALOG, model_id)
    if row is None:
        raise not_found()
    logger.info("catalog model %s set %s by %s", model_id, status, actor)
    return _entry(row)


async def delete(model_id: uuid.UUID, actor: str) -> None:
    async with (await get_pool()).acquire() as conn, conn.transaction():
        if await conn.fetchval(_DELETE_UNUSED, model_id) is None:
            if await conn.fetchval(_EXISTS, model_id):
                raise Problem(HTTPStatus.CONFLICT, ProblemCode.IN_USE, "a training preset still pins this model; disable it instead")
            raise not_found()
        await admin_audit.record(conn, actor, AdminAuditAction.MODEL_CATALOG_DELETE, AdminAuditEntity.MODEL_CATALOG, model_id)
    logger.info("catalog model %s deleted by %s", model_id, actor)
