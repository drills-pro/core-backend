"""MLflow-backed model versions and stage promotion."""
from app.modules.user.model_registry.constants import MODEL_NAME_PATTERN
from app.modules.user.model_registry.routes import router
from app.modules.user.model_registry.schemas import ModelVersion, RegisteredVersion, VersionRef
from app.modules.user.model_registry.service import (
    get_run_version,
    record_version,
    register_in_mlflow,
    register_run,
)

__all__ = [
    "MODEL_NAME_PATTERN",
    "ModelVersion",
    "RegisteredVersion",
    "VersionRef",
    "get_run_version",
    "record_version",
    "register_in_mlflow",
    "register_run",
    "router",
]
