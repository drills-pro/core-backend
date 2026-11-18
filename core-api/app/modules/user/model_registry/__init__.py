"""MLflow-backed model versions and stage promotion."""
from app.modules.user.model_registry.constants import MODEL_NAME_PATTERN, PROMOTION_TENANTS_SQL, PromotionStatus
from app.modules.user.model_registry.routes import router
from app.modules.user.model_registry.schemas import ModelVersion, RegisteredVersion, VersionRef
from app.modules.user.model_registry.service import (
    complete_promotion,
    fail_promotion,
    get_run_version,
    pending_promotions,
    record_version,
    register_in_mlflow,
    register_run,
    serving_version,
)

__all__ = [
    "MODEL_NAME_PATTERN",
    "PROMOTION_TENANTS_SQL",
    "ModelVersion",
    "PromotionStatus",
    "RegisteredVersion",
    "VersionRef",
    "complete_promotion",
    "fail_promotion",
    "get_run_version",
    "pending_promotions",
    "record_version",
    "register_in_mlflow",
    "register_run",
    "router",
    "serving_version",
]
