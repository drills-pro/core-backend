"""Named values for the model registry."""
from app.integrations.data_plane.mlflow import ModelStage

# req/06 FR2: registry stages map to customer-facing states.
CUSTOMER_STAGE_LABELS = {
    ModelStage.NONE: "internal only",
    ModelStage.STAGING: "candidate",
    ModelStage.PRODUCTION: "live/deployed",
}
MODEL_NAME_PATTERN = r"^[A-Za-z0-9_-]{1,60}$"  # a registered model's name as the customer chooses it (MLflow's full name is namespaced)

NOTHING_TO_UPDATE_DETAIL = "nothing to update"
BAD_STAGE_DETAIL = f"stage must be one of {sorted(ModelStage)}"
