"""MLflow-backed model versions and stage promotion."""
from app.modules.user.model_registry.routes import router
from app.modules.user.model_registry.service import register_version

__all__ = ["register_version", "router"]
