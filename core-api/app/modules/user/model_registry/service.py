"""Model versions: register a run's MLflow model, list, promote between stages, publish flags.

Registering is two steps with no pooled connection held across the first: `register_in_mlflow` (the gateway) and
`record_version` (the model_versions row). The training settle step uses them too, so every succeeded run has
its row written once it is the compare-and-set winner.
"""
import uuid
from http import HTTPStatus

from app.core import audit
from app.core.authz import AuthzContext
from app.core.problem import Problem, not_found
from app.core.tenant_conn import TenantConn
from app.integrations.data_plane import mlflow
from app.integrations.data_plane.mlflow import ModelStage
from app.modules.user.model_registry.constants import (
    CUSTOMER_STAGE_LABELS,
    AuditAction,
    EntityType,
    NOTHING_TO_UPDATE_DETAIL,
    DownloadFile,
    ProblemCode,
)
from app.modules.user.model_registry.schemas import (
    ModelVersion,
    ModelVersionItem,
    PromotedVersion,
    PublishState,
    RegisteredVersion,
    VersionRef,
)

# The model (planning/24 Q1) is one row per (project, registered name); a version joins the one its name has.
_ENSURE_MODEL = "INSERT INTO models (tenant_id, project_id, name) VALUES ($1, $2, $3) ON CONFLICT (project_id, name) DO NOTHING"
_INSERT = (
    "INSERT INTO model_versions (tenant_id, project_id, training_run_id, mlflow_model_name, mlflow_version, model_id) "
    "SELECT $1, $2, $3, $4, $5, (SELECT id FROM models WHERE project_id = $2 AND name = $4) "
    # a retried register answers the row the first one wrote (the no-op update makes RETURNING see it)
    "ON CONFLICT (project_id, mlflow_model_name, mlflow_version) DO UPDATE SET mlflow_version = EXCLUDED.mlflow_version "
    "RETURNING id, mlflow_model_name, mlflow_version, stage, created_at"
)
_LIST = (
    "SELECT id, model_id, mlflow_model_name, mlflow_version, stage, created_at FROM model_versions "
    "WHERE project_id = $1 ORDER BY created_at DESC"
)
_BY_ID = "SELECT mlflow_model_name, mlflow_version, stage FROM model_versions WHERE id = $1 AND project_id = $2"
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
    name = mlflow.display_name(model.mlflow_model_name, tenant_id, str(project_id))
    return model.model_copy(update={"mlflow_model_name": name})


async def register_in_mlflow(tenant_id: str, project_id: uuid.UUID, name: str, mlflow_run_id: str) -> RegisteredVersion:
    """Create the MLflow model version; takes no connection."""
    # Registered model names are global in MLflow, so the tenant and project are part of the name.
    return RegisteredVersion(
        mlflow_model_name=mlflow.registered_model_name(tenant_id, str(project_id), name),
        mlflow_version=await mlflow.register_model_version(tenant_id, str(project_id), name, mlflow_run_id),
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


async def promote(ctx: AuthzContext, project_id: uuid.UUID, version_id: uuid.UUID, stage: ModelStage) -> PromotedVersion:
    """None (internal) -> Staging (candidate) -> Production (live). Also the rollback path: promoting an older version
    to Production is a rollback (MLflow does not tell the two apart, and neither does this). MLflow is called with no
    connection held."""
    tenant_id = ctx.tenant_id
    async with TenantConn(tenant_id) as conn:
        row = await conn.fetchrow(_BY_ID, version_id, project_id)
    if row is None:
        raise not_found()
    name = mlflow.display_name(row["mlflow_model_name"], tenant_id, str(project_id))
    await mlflow.transition_stage(tenant_id, str(project_id), name, row["mlflow_version"], stage)
    try:
        async with TenantConn(tenant_id) as conn:
            updated = await conn.fetchrow(_SET_STAGE, version_id, stage)
            if updated is not None:
                await audit.record_for(conn, ctx, project_id, AuditAction.PROMOTE, EntityType.MODEL_VERSION, version_id)
    except Exception:
        # The row write failed after MLflow moved: put MLflow back so the two do not diverge (audit C-14).
        await mlflow.transition_stage(tenant_id, str(project_id), name, row["mlflow_version"], ModelStage(row["stage"]))
        raise
    if updated is None:
        raise not_found()
    shown = _shown(VersionRef.model_validate(dict(updated)), tenant_id, project_id)
    return PromotedVersion(**shown.model_dump(), customer_label=_label(shown.stage))


async def set_publish(
    ctx: AuthzContext, project_id: uuid.UUID, version_id: uuid.UUID, metadata: bool | None, artifact: bool | None
) -> PublishState:
    # FR7 (08-collaboration-visibility.md): metrics/config vs. the weights artifact are separate disclosures,
    # independently toggleable.
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
    return await mlflow.open_model_file(ctx.tenant_id, str(project_id), mlflow_run_id, file)
