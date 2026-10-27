"""Registering a succeeded training run's MLflow run as a model version: shared by the register route and the
training reconciler, which registers every run that succeeds."""
import uuid

from app.integrations.data_plane import mlflow


async def register_version(conn, tenant_id: str, project_id: uuid.UUID, training_run_id: uuid.UUID, mlflow_run_id: str, name: str):
    """Create the MLflow model version and its model_versions row; returns the row (id, mlflow_model_name, mlflow_version, stage, created_at)."""
    # Registered model names are global in MLflow, so the tenant and project are part of the name.
    mlflow_name = mlflow.registered_model_name(tenant_id, str(project_id), name)
    mlflow_version = await mlflow.register_model_version(tenant_id, str(project_id), name, mlflow_run_id)
    return await conn.fetchrow(
        "INSERT INTO model_versions (tenant_id, project_id, training_run_id, mlflow_model_name, mlflow_version) "
        "VALUES ($1, $2, $3, $4, $5) RETURNING id, mlflow_model_name, mlflow_version, stage, created_at",
        tenant_id,
        project_id,
        training_run_id,
        mlflow_name,
        mlflow_version,
    )
