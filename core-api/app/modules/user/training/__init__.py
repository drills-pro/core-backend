"""Training runs on Ray, and their lifecycle transitions."""
from app.integrations.data_plane import OwnedResource, register_ownership_check
from app.modules.user.training.constants import TrainingStatus
from app.modules.user.training.readers import RunModel, get_run_lineage, get_run_model, owns_training_run
from app.modules.user.training.schemas import LineageRun
from app.modules.user.training.routes import events_router, presets_router, router
from app.modules.user.training.service import reconcile_inflight_runs

register_ownership_check(OwnedResource.TRAINING_RUN, owns_training_run)

__all__ = ["LineageRun", "RunModel", "TrainingStatus", "get_run_lineage", "get_run_model", "events_router", "presets_router", "reconcile_inflight_runs", "router"]
