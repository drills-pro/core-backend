"""Named values for deployments and inference."""
from enum import StrEnum

from app.core.enums import PlanTier


class Serving(StrEnum):
    """How a deployment is served. Mirrors the deployments_serving_check constraint."""

    LEGACY = "legacy"  # a Serve app of its own at route_prefix, started by a Ray job
    SHARED = "shared"  # a model the two long-lived predictor apps load by id


class DeployStrategy(StrEnum):
    """Project setting `deploy_strategy`."""

    LATEST = "latest"
    MAIN = "main"


# req/04 FR4: rate limit "configurable per tenant plan tier."
PLAN_TIER_RATE_LIMITS = {PlanTier.FREE: 10, PlanTier.PRO: 120, PlanTier.ENTERPRISE: 1000}

DEFAULT_COMPUTE_TARGET = "cpu"
SETTING_DEPLOY_STRATEGY = "deploy_strategy"
SETTING_COMPUTE_TARGET = "compute_target"
MAIN_ROUTE_SUFFIX = "main"
PREDICT_KEY_BYTES = 32
PREDICT_KEY_HEADER = "X-Predict-Key"
METADATA_FIELD = "metadata"
SERVE_TIMEOUT_SECONDS = 10
TEST_CALLS_DEFAULT_LIMIT = 20

NOT_DEPLOYABLE_DETAIL = (
    "training run has no deployable model (only a succeeded run of a servable task type can be served)"
)
