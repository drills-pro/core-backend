"""Model versions: register a run's MLflow model, list, promote between stages, publish flags.

Promoting to Production is asked for here and settled by the deployments' promotion worker (planning/27): `promote` marks the version
pending, the worker deploys it, and only when that deployment is active does `complete_promotion` flip the stages (the old Production
version becomes Staging, so a model has one Production). A failed deploy is `fail_promotion`: no stage moves, the error stays on the version.

Registering is two steps with no pooled connection held across the first: `register_in_mlflow` (the gateway) and
`record_version` (the model_versions row). The training settle step uses them too, so every succeeded run has
its row written once it is the compare-and-set winner.
"""
import logging
import uuid
from http import HTTPStatus

import asyncpg

from app.core import audit
from app.core.authz import AuthzContext
from app.core.problem import Problem, not_found
from app.core.tenant_conn import TenantConn
from app.integrations import data_plane
from app.integrations.data_plane import ModelStage
from app.modules.user.model_registry.constants import (
    CUSTOMER_STAGE_LABELS,
    AuditAction,
    EntityType,
    LIVE_VERSION_DETAIL,
    NOTHING_TO_UPDATE_DETAIL,
    PROMOTION_EVENT_FAILED,
    PROMOTION_EVENT_PROMOTED,
    PROMOTION_IN_PROGRESS_DETAIL,
    DownloadFile,
    ProblemCode,
    PromotionStatus,
)
from app.modules.user.model_registry.schemas import (
    ModelVersion,
    ModelVersionItem,
    PromotedVersion,
    PublishState,
    RegisteredVersion,
    VersionRef,
)

logger = logging.getLogger(__name__)

# The model (planning/24 Q1) is one row per (project, registered name); a version joins the one its name has.
_ENSURE_MODEL = "INSERT INTO models (tenant_id, project_id, name) VALUES ($1, $2, $3) ON CONFLICT (project_id, name) DO NOTHING"
_INSERT = (
    "INSERT INTO model_versions (tenant_id, project_id, training_run_id, mlflow_model_name, mlflow_version, model_id) "
    "SELECT $1, $2, $3, $4, $5, (SELECT id FROM models WHERE project_id = $2 AND name = $4) "
    # a retried register answers the row the first one wrote (the no-op update makes RETURNING see it)
    "ON CONFLICT (project_id, mlflow_model_name, mlflow_version) DO UPDATE SET mlflow_version = EXCLUDED.mlflow_version "
    "RETURNING id, mlflow_model_name, mlflow_version, stage, created_at"
)
_PROMOTION_COLUMNS = "promotion_status, promotion_error, promotion_error_code"
_LIST = (
    "SELECT id, model_id, training_run_id, mlflow_model_name, mlflow_version, stage, created_at, publish_metadata, publish_artifact, "
    f"{_PROMOTION_COLUMNS} FROM model_versions WHERE project_id = $1 ORDER BY created_at DESC"
)
_BY_ID = (
    f"SELECT id, model_id, mlflow_model_name, mlflow_version, stage, created_at, {_PROMOTION_COLUMNS} FROM model_versions "
    "WHERE id = $1 AND project_id = $2"
)
_STATE = f"SELECT id, mlflow_model_name, mlflow_version, stage, {_PROMOTION_COLUMNS} FROM model_versions WHERE id = $1 AND project_id = $2"
# Asking for Production: pending unless one is already (the unique index allows one pending per model).
_REQUEST_PRODUCTION = (
    "UPDATE model_versions SET promotion_status = $3, promotion_error = NULL, promotion_error_code = NULL, promotion_actor = $4, promotion_acknowledged_risk = $5, "
    "promotion_requested_at = now() WHERE id = $1 AND project_id = $2 AND stage <> $6 AND promotion_status IS DISTINCT FROM $3 "
    f"RETURNING id, mlflow_model_name, mlflow_version, stage, {_PROMOTION_COLUMNS}"
)
_PENDING = (
    "SELECT id, project_id, promotion_actor, promotion_acknowledged_risk FROM model_versions "
    "WHERE tenant_id = $1 AND promotion_status = $2 ORDER BY promotion_requested_at"
)
_PRODUCTION_OF_MODEL = "SELECT id, mlflow_model_name, mlflow_version, created_at FROM model_versions WHERE model_id = $1 AND stage = $2 AND id <> $3"
_SERVING_OF = "SELECT p.id FROM model_versions v JOIN model_versions p ON p.model_id = v.model_id AND p.stage = $2 AND p.id <> v.id WHERE v.id = $1 LIMIT 1"
_DEMOTE = "UPDATE model_versions SET stage = $3 WHERE model_id = $1 AND stage = $2 AND id <> $4"
_PROMOTED = (
    "UPDATE model_versions SET stage = $2, promotion_status = NULL, promotion_error = NULL, promotion_error_code = NULL, promotion_actor = NULL, "
    "promotion_requested_at = NULL WHERE id = $1"
)
_FAILED = (
    "UPDATE model_versions SET promotion_status = $3, promotion_error = $4, promotion_error_code = $5 WHERE id = $1 AND project_id = $2 AND promotion_status = $6 "
    "RETURNING id"
)
_EMIT = "INSERT INTO events (tenant_id, project_id, kind, entity_id, status) VALUES ($1, $2, $3, $4, $5)"
_RUN_OF = (
    "SELECT tr.mlflow_run_id FROM model_versions mv JOIN training_runs tr ON tr.id = mv.training_run_id "
    "WHERE mv.id = $1 AND mv.project_id = $2"
)
_SET_STAGE = "UPDATE model_versions SET stage = $2 WHERE id = $1 RETURNING id, mlflow_model_name, mlflow_version, stage"
_SET_PUBLISH = (
    "UPDATE model_versions SET publish_metadata = COALESCE($2, publish_metadata), "
    "publish_artifact = COALESCE($3, publish_artifact) WHERE id = $1 AND project_id = $4 "
    "RETURNING id, publish_metadata, publish_artifact"
)
_LATEST_OF_RUN = (
    "SELECT id, mlflow_model_name, mlflow_version, stage FROM model_versions "
    "WHERE training_run_id = $1 ORDER BY created_at DESC LIMIT 1"
)


def _label(stage: ModelStage) -> str:
    return CUSTOMER_STAGE_LABELS[stage]


def _shown[M: VersionRef](model: M, tenant_id: str, project_id: uuid.UUID) -> M:
    """Customers see the name they chose, not the tenant/project-namespaced MLflow name."""
    name = data_plane.display_name(model.mlflow_model_name, tenant_id, str(project_id))
    return model.model_copy(update={"mlflow_model_name": name})


async def register_in_mlflow(tenant_id: str, project_id: uuid.UUID, name: str, mlflow_run_id: str) -> RegisteredVersion:
    """Create the MLflow model version; takes no connection."""
    # Registered model names are global in MLflow, so the tenant and project are part of the name.
    return RegisteredVersion(
        mlflow_model_name=data_plane.registered_model_name(tenant_id, str(project_id), name),
        mlflow_version=await data_plane.register_model_version(tenant_id, str(project_id), name, mlflow_run_id),
    )


async def record_version(
    conn, tenant_id: str, project_id: uuid.UUID, training_run_id: uuid.UUID, registered: RegisteredVersion
) -> ModelVersion:
    await conn.execute(_ENSURE_MODEL, tenant_id, project_id, registered.mlflow_model_name)
    row = await conn.fetchrow(
        _INSERT, tenant_id, project_id, training_run_id, registered.mlflow_model_name, registered.mlflow_version
    )
    return ModelVersion.model_validate(dict(row))


async def register_run(
    ctx: AuthzContext, project_id: uuid.UUID, training_run_id: uuid.UUID, name: str, mlflow_run_id: str
) -> ModelVersion:
    """Register a run's model; the caller (training) has already checked the run is registrable."""
    registered = await register_in_mlflow(ctx.tenant_id, project_id, name, mlflow_run_id)
    async with TenantConn(ctx.tenant_id) as conn:
        stored = await record_version(conn, ctx.tenant_id, project_id, training_run_id, registered)
        await audit.record_for(conn, ctx, project_id, AuditAction.REGISTER, EntityType.MODEL_VERSION, stored.id)
    return _shown(stored, ctx.tenant_id, project_id)


async def list_versions(ctx: AuthzContext, project_id: uuid.UUID) -> list[ModelVersionItem]:
    async with TenantConn(ctx.tenant_id) as conn:
        rows = await conn.fetch(_LIST, project_id)
    return [
        _shown(ModelVersionItem.model_validate({**dict(r), "customer_label": _label(r["stage"])}), ctx.tenant_id, project_id)
        for r in rows
    ]


async def promote(
    ctx: AuthzContext, project_id: uuid.UUID, version_id: uuid.UUID, stage: ModelStage, acknowledge_risk: bool = False,
) -> PromotedVersion:
    """None (internal) -> Staging (candidate) is immediate. Production is asked for, not done: the version goes pending, the worker
    deploys it and flips the stage when that deployment is active (a rollback is the same promotion of an older version). The version
    that is serving is not moved down on its own: promote another one. MLflow is called with no connection held."""
    if stage == ModelStage.PRODUCTION:
        return await _request_production(ctx, project_id, version_id, acknowledge_risk)
    tenant_id = ctx.tenant_id
    async with TenantConn(tenant_id) as conn:
        row = await conn.fetchrow(_BY_ID, version_id, project_id)
    if row is None:
        raise not_found()
    if row["stage"] == ModelStage.PRODUCTION:
        raise Problem(HTTPStatus.CONFLICT, ProblemCode.LIVE_VERSION, LIVE_VERSION_DETAIL)
    if row["promotion_status"] == PromotionStatus.PENDING:
        raise Problem(HTTPStatus.CONFLICT, ProblemCode.PROMOTION_IN_PROGRESS, PROMOTION_IN_PROGRESS_DETAIL)
    name = data_plane.display_name(row["mlflow_model_name"], tenant_id, str(project_id))
    await data_plane.transition_stage(tenant_id, str(project_id), name, row["mlflow_version"], stage)
    try:
        async with TenantConn(tenant_id) as conn:
            updated = await conn.fetchrow(_SET_STAGE, version_id, stage)
            if updated is not None:
                await audit.record_for(conn, ctx, project_id, AuditAction.PROMOTE, EntityType.MODEL_VERSION, version_id)
    except Exception:
        # The row write failed after MLflow moved: put MLflow back so the two do not diverge (audit C-14).
        await data_plane.transition_stage(tenant_id, str(project_id), name, row["mlflow_version"], ModelStage(row["stage"]))
        raise
    if updated is None:
        raise not_found()
    shown = _shown(VersionRef.model_validate(dict(updated)), tenant_id, project_id)
    return PromotedVersion(**shown.model_dump(), customer_label=_label(shown.stage))


async def _request_production(ctx: AuthzContext, project_id: uuid.UUID, version_id: uuid.UUID, acknowledge_risk: bool) -> PromotedVersion:
    """Mark the version pending for the worker. Asking again while it is pending, or once it is Production, answers its state as it is."""
    try:
        async with TenantConn(ctx.tenant_id) as conn:
            row = await conn.fetchrow(
                _REQUEST_PRODUCTION, version_id, project_id, PromotionStatus.PENDING, ctx.actor, acknowledge_risk, ModelStage.PRODUCTION)
            if row is None:
                row = await conn.fetchrow(_STATE, version_id, project_id)
    except asyncpg.UniqueViolationError:  # model_versions_one_pending_per_model
        raise Problem(HTTPStatus.CONFLICT, ProblemCode.PROMOTION_IN_PROGRESS, PROMOTION_IN_PROGRESS_DETAIL) from None
    if row is None:
        raise not_found()
    shown = _shown(VersionRef.model_validate(dict(row)), ctx.tenant_id, project_id)
    return PromotedVersion(**shown.model_dump(), customer_label=_label(shown.stage), **_promotion_of(row))


def _promotion_of(row) -> dict:
    return {key: row[key] for key in ("promotion_status", "promotion_error", "promotion_error_code")}


async def pending_promotions(conn, tenant_id: str) -> list:
    """The tenant's promotions the worker has not settled: (id, project_id, promotion_actor, promotion_acknowledged_risk), oldest first."""
    return await conn.fetch(_PENDING, tenant_id, PromotionStatus.PENDING)


async def serving_version(conn, version_id: uuid.UUID) -> uuid.UUID | None:
    """The Production version of the model `version_id` belongs to (another one), if it has one: what keeps serving until a promotion settles."""
    return await conn.fetchval(_SERVING_OF, version_id, ModelStage.PRODUCTION)


async def _restore(tenant_id: str, project_id: uuid.UUID, moved: list[tuple[str, str, ModelStage]]) -> None:
    """Put MLflow's stages back as they were, newest move first; one that cannot be put back is logged, not raised over the real error."""
    for name, version, previous in reversed(moved):
        try:
            await data_plane.transition_stage(tenant_id, str(project_id), name, version, previous)
        except Exception:  # noqa: BLE001
            logger.exception("could not put %s version %s back to %s", name, version, previous)


async def complete_promotion(ctx: AuthzContext, project_id: uuid.UUID, version_id: uuid.UUID) -> None:
    """The version's deployment is active: flip the stages. Every other Production version of the model goes to Staging, this one to
    Production, in MLflow and then in one transaction (with the audit row and the outbox event). A version older than the one it
    replaces is a rollback. Any failure puts MLflow back and is raised, leaving every stage as it was."""
    tenant_id = ctx.tenant_id
    async with TenantConn(tenant_id) as conn:
        row = await conn.fetchrow(_BY_ID, version_id, project_id)
        if row is None:
            raise not_found()
        replaced = await conn.fetch(_PRODUCTION_OF_MODEL, row["model_id"], ModelStage.PRODUCTION, version_id)
    rollback = any(old["created_at"] > row["created_at"] for old in replaced)
    moved: list[tuple[str, str, ModelStage]] = []
    try:
        if row["stage"] != ModelStage.PRODUCTION:  # a settled earlier try already moved it
            for old in replaced:
                name = data_plane.display_name(old["mlflow_model_name"], tenant_id, str(project_id))
                await data_plane.transition_stage(tenant_id, str(project_id), name, old["mlflow_version"], ModelStage.STAGING)
                moved.append((name, old["mlflow_version"], ModelStage.PRODUCTION))
            name = data_plane.display_name(row["mlflow_model_name"], tenant_id, str(project_id))
            await data_plane.transition_stage(tenant_id, str(project_id), name, row["mlflow_version"], ModelStage.PRODUCTION)
            moved.append((name, row["mlflow_version"], ModelStage(row["stage"])))
        async with TenantConn(tenant_id) as conn:
            await conn.execute(_DEMOTE, row["model_id"], ModelStage.PRODUCTION, ModelStage.STAGING, version_id)
            await conn.execute(_PROMOTED, version_id, ModelStage.PRODUCTION)
            await audit.record_for(
                conn, ctx, project_id, AuditAction.ROLLBACK if rollback else AuditAction.PROMOTE, EntityType.MODEL_VERSION, version_id)
            await conn.execute(_EMIT, tenant_id, project_id, PROMOTION_EVENT_PROMOTED, version_id, ModelStage.PRODUCTION)
    except Exception:
        await _restore(tenant_id, project_id, moved)
        raise


async def fail_promotion(ctx: AuthzContext, project_id: uuid.UUID, version_id: uuid.UUID, error: str, code: str | None = None) -> None:
    """The promotion could not finish: the version keeps its stage, the error (and its problem code) shows on it and the outbox says so."""
    async with TenantConn(ctx.tenant_id) as conn:
        failed = await conn.fetchrow(_FAILED, version_id, project_id, PromotionStatus.FAILED, error, code, PromotionStatus.PENDING)
        if failed is not None:
            await conn.execute(_EMIT, ctx.tenant_id, project_id, PROMOTION_EVENT_FAILED, version_id, PromotionStatus.FAILED)


async def set_publish(
    ctx: AuthzContext, project_id: uuid.UUID, version_id: uuid.UUID, metadata: bool | None, artifact: bool | None
) -> PublishState:
    # FR7 (08-collaboration-visibility.md): metrics/config vs. the weights artifact are separate disclosures.
    # Since project sharing (planning/27 #4) these only exclude one version: the project's level decides the rest.
    if metadata is None and artifact is None:
        raise Problem(HTTPStatus.BAD_REQUEST, ProblemCode.NOTHING_TO_UPDATE, NOTHING_TO_UPDATE_DETAIL)
    async with TenantConn(ctx.tenant_id) as conn:
        row = await conn.fetchrow(_SET_PUBLISH, version_id, metadata, artifact, project_id)
        if row is not None:
            await audit.record_for(conn, ctx, project_id, AuditAction.SET_PUBLISH, EntityType.MODEL_VERSION, version_id)
    if row is None:
        raise not_found()
    return PublishState.model_validate(dict(row))


async def get_run_version(conn, training_run_id: uuid.UUID) -> VersionRef | None:
    """The newest version registered from a training run (the lineage view), with MLflow's namespaced name."""
    row = await conn.fetchrow(_LATEST_OF_RUN, training_run_id)
    return None if row is None else VersionRef.model_validate(dict(row))


async def open_download(ctx: AuthzContext, project_id: uuid.UUID, version_id: uuid.UUID, file: DownloadFile):
    """The version's model file as an open stream (client, response) for the route to forward, and an audit row."""
    async with TenantConn(ctx.tenant_id) as conn:
        mlflow_run_id = await conn.fetchval(_RUN_OF, version_id, project_id)
        if mlflow_run_id is None:
            raise not_found()
        await audit.record_for(conn, ctx, project_id, AuditAction.DOWNLOAD, EntityType.MODEL_VERSION, version_id)
    return await data_plane.open_model_file(ctx.tenant_id, str(project_id), mlflow_run_id, file)
