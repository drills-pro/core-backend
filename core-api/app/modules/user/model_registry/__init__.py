"""MLflow-backed model versions and stage promotion."""
from app.modules.user.model_registry.constants import MODEL_NAME_PATTERN
from app.modules.user.model_registry.routes import router
from app.modules.user.model_registry.schemas import ModelVersion, PendingPromotion, RegisteredVersion
from app.modules.user.model_registry.service import (
    complete_promotion,
    fail_promotion,
    get_run_version,
    pending_promotion_tenants,
    pending_promotions,
    record_version,
    register_in_mlflow,
    register_run,
    serving_version,
)

__all__ = [
    "MODEL_NAME_PATTERN",
    "ModelVersion",
    "PendingPromotion",
    "RegisteredVersion",
    "complete_promotion",
    "fail_promotion",
    "get_run_version",
    "pending_promotion_tenants",
    "pending_promotions",
    "record_version",
    "register_in_mlflow",
    "register_run",
    "router",
    "serving_version",
]
