"""Model versions: every succeeded training run adds one version to its project's registered model (named by the project id).
The version is MLflow's own number; the run it came from is its source run, and the tags say which training run and project.

Registering is two steps with no pooled connection held across the first: `register_in_mlflow` (the gateway) and
`record_version` (the model_versions row). The training settle step uses them, so every succeeded run has its row written
once it is the compare-and-set winner. Nothing else creates a version.
"""
import uuid

from app.core import audit
from app.core.authz import AuthzContext
from app.core.problem import not_found
from app.core.tenant_conn import TenantConn
from app.integrations import data_plane
from app.modules.user.model_registry.constants import (
    AuditAction,
    DownloadFile,
    EntityType,
)
from app.modules.user.model_registry.schemas import ModelVersion, ModelVersionItem, RegisteredVersion, VersionRef

_ENSURE_MODEL = "INSERT INTO models (tenant_id, project_id, name) VALUES ($1, $2, $3) ON CONFLICT (project_id, name) DO NOTHING"
_INSERT = (
    "INSERT INTO model_versions (tenant_id, project_id, training_run_id, mlflow_model_name, mlflow_version, model_id) "
    "SELECT $1, $2, $3, $4, $5, (SELECT id FROM models WHERE project_id = $2 AND name = $4) "
    # a retried register answers the row the first one wrote (the no-op update makes RETURNING see it)
    "ON CONFLICT (project_id, mlflow_model_name, mlflow_version) DO UPDATE SET mlflow_version = EXCLUDED.mlflow_version "
    "RETURNING id, mlflow_model_name, mlflow_version, created_at"
)
_LIST = (
    "SELECT id, model_id, mlflow_model_name, mlflow_version, created_at FROM model_versions "
    "WHERE project_id = $1 ORDER BY created_at DESC"
)
_RUN_OF = (
    "SELECT tr.mlflow_run_id FROM model_versions mv JOIN training_runs tr ON tr.id = mv.training_run_id "
    "WHERE mv.id = $1 AND mv.project_id = $2"
)
_LATEST_OF_RUN = (
    "SELECT id, mlflow_model_name, mlflow_version FROM model_versions "
    "WHERE training_run_id = $1 ORDER BY created_at DESC LIMIT 1"
)


def _shown(model, tenant_id: str, project_id: uuid.UUID):
    """Customers see the project's name, not the tenant/project-namespaced MLflow name."""
    name = data_plane.display_name(model.mlflow_model_name, tenant_id, str(project_id))
    return model.model_copy(update={"mlflow_model_name": name})


async def register_in_mlflow(tenant_id: str, project_id: uuid.UUID, name: str, mlflow_run_id: str, training_run_id: str) -> RegisteredVersion:
    """Create the MLflow model version, tagged with its training run; takes no connection."""
    # Registered model names are global in MLflow, so the tenant and project are part of the name.
    return RegisteredVersion(
        mlflow_model_name=data_plane.registered_model_name(tenant_id, str(project_id), name),
        mlflow_version=await data_plane.register_model_version(tenant_id, str(project_id), name, mlflow_run_id, training_run_id),
    )


async def record_version(
    conn, tenant_id: str, project_id: uuid.UUID, training_run_id: uuid.UUID, registered: RegisteredVersion
) -> ModelVersion:
    await conn.execute(_ENSURE_MODEL, tenant_id, project_id, registered.mlflow_model_name)
    row = await conn.fetchrow(_INSERT, tenant_id, project_id, training_run_id, registered.mlflow_model_name, registered.mlflow_version)
    return ModelVersion.model_validate(dict(row))


async def list_versions(ctx: AuthzContext, project_id: uuid.UUID) -> list[ModelVersionItem]:
    async with TenantConn(ctx.tenant_id) as conn:
        rows = await conn.fetch(_LIST, project_id)
    return [_shown(ModelVersionItem.model_validate(dict(r)), ctx.tenant_id, project_id) for r in rows]


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
