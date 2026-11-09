"""Named values for deployments and inference."""
from enum import StrEnum
from http import HTTPStatus

from app.core.enums import PlanTier
from app.integrations.serve_client import ServeErrorCode


class Serving(StrEnum):
    """How a deployment is served. Mirrors the deployments_serving_check constraint."""

    LEGACY = "legacy"  # a Serve app of its own at route_prefix, started by a Ray job
    SHARED = "shared"  # a model the two long-lived predictor apps load by id


class ProblemCode(StrEnum):
    BODY_TOO_LARGE = "body_too_large"
    BODY_NOT_JSON = "body_not_json"
    BODY_NOT_OBJECT = "body_not_object"
    NOT_DEPLOYABLE = "not_deployable"
    NOT_ACTIVE = "deployment_not_active"
    STOPPED = "deployment_stopped"
    INVALID_TRANSITION = "invalid_deployment_transition"
    NO_SERVABLE_MODEL = "no_servable_model"
    INVALID_PREDICT_KEY = "invalid_predict_key"
    RATE_LIMITED = "rate_limited"
    TOO_MANY_INFLIGHT = "too_many_inflight"
    TOO_MANY_DEPLOYMENTS = "too_many_deployments"
    DEPLOYMENT_EXISTS = "deployment_exists"
    IDEMPOTENCY_MISMATCH = "idempotency_mismatch"
    MODEL_REJECTED_INPUT = "model_rejected_input"
    UPSTREAM_ERROR = "upstream_error"
    UPSTREAM_TIMEOUT = "upstream_timeout"
    IDEMPOTENCY_KEY_REUSE = "idempotency_key_reuse"
    REQUEST_IN_PROGRESS = "request_in_progress"


class PredictionStatus(StrEnum):
    """Mirrors the predictions.status and prediction_events.status CHECK constraints."""

    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PredictionMode(StrEnum):
    """Mirrors the predictions.mode CHECK constraint: who owns the row while it runs."""

    SYNC = "sync"  # the request handler (a sync call sent with an Idempotency-Key)
    BACKGROUND = "background"  # the prediction worker


TERMINAL_STATUSES = frozenset({PredictionStatus.COMPLETED, PredictionStatus.FAILED, PredictionStatus.CANCELLED})


class AuditAction(StrEnum):
    STOP = "stop"
    START = "start"
    DELETE = "delete"


class EntityType(StrEnum):
    DEPLOYMENT = "deployment"


class DeployStrategy(StrEnum):
    """Project setting `deploy_strategy`."""

    LATEST = "latest"
    MAIN = "main"


# req/04 FR4: rate limit "configurable per tenant plan tier."
PLAN_TIER_RATE_LIMITS = {PlanTier.FREE: 10, PlanTier.PRO: 120, PlanTier.ENTERPRISE: 1000, PlanTier.INTERNAL: 1000}

DEFAULT_COMPUTE_TARGET = "cpu"
SETTING_DEPLOY_STRATEGY = "deploy_strategy"
SETTING_COMPUTE_TARGET = "compute_target"
MAIN_ROUTE_SUFFIX = "main"
PREDICT_KEY_BYTES = 32
PREDICT_KEY_HEADER = "X-Predict-Key"
METADATA_FIELD = "metadata"
PREDICTIONS_FIELD = "predictions"
SERVE_TIMEOUT_SECONDS = 10
TEST_CALLS_DEFAULT_LIMIT = 20
TEST_CALLS_MIN_LIMIT = 1
TEST_CALLS_MAX_LIMIT = 100
TEST_CALL_STORED_MAX_BYTES = 65536  # a try-it request or response kept in the log; a bigger one is replaced by a marker
TRUNCATED_FIELD = "truncated"
TRUNCATED_BYTES_FIELD = "bytes"
DEPLOYMENT_LOCK_PREFIX = "deployments:"  # advisory-lock key prefix: serialises the quota check and the write per tenant
DEPLOYMENT_EXISTS_DETAIL = "this training run already has a deployment; use it (start it if it is stopped) or delete it first"
TOO_MANY_DEPLOYMENTS_DETAIL = "too many active deployments for this plan, stop or delete one first"
STOPPED_DETAIL = "the deployment is stopped; start it to serve again"
TOO_MANY_REQUESTS_DETAIL = "too many concurrent requests for this deployment"
TARGET_CACHE_SIZE = 1024  # (deployment, run) pairs whose model URLs core-api remembers

NOT_DEPLOYABLE_DETAIL = (
    "training run has no deployable model (only a succeeded run of a servable task type can be served)"
)

# What a failed call to the shared predictor apps is answered with (serve_client.ServeErrorCode).
SERVE_ERROR_STATUS = {
    ServeErrorCode.BAD_INPUT: HTTPStatus.UNPROCESSABLE_ENTITY,
    ServeErrorCode.MODEL_LOAD_FAILED: HTTPStatus.BAD_GATEWAY,
    ServeErrorCode.UNKNOWN_FUNCTION: HTTPStatus.BAD_GATEWAY,
    ServeErrorCode.BAD_ANSWER: HTTPStatus.BAD_GATEWAY,
    ServeErrorCode.UNREACHABLE: HTTPStatus.SERVICE_UNAVAILABLE,
    ServeErrorCode.TIMEOUT: HTTPStatus.GATEWAY_TIMEOUT,
}

# POST /v1/predict (planning/24): the model is addressed by headers, the caller's data is `input`.
MODEL_ID_HEADER = "X-Model-Id"
MODEL_REVISION_HEADER = "X-Model-Revision"
PREDICTION_ID_PREFIX = "pred_"
METADATA_MAX_BYTES = 4096
MODEL_REJECTED_DETAIL_MAX_CHARS = 500
PLAN_RATE_LIMIT_KEY = "plan:{}"  # rate_limit.check keys of POST /v1/predict: the tenant's plan limit and one API key's own
KEY_RATE_LIMIT_KEY = "key:{}"
RATE_LIMIT_WINDOW_SECONDS = 60  # rate_limit.check counts fixed one-minute windows
UPSTREAM_ERROR_DETAIL = "the model could not answer; try again"
UPSTREAM_TIMEOUT_DETAIL = "the model did not answer in time"
METADATA_TOO_LARGE_DETAIL = f"metadata is over {METADATA_MAX_BYTES} bytes"

# Stored predictions (planning/24): Idempotency-Key, background mode.
PREFER_HEADER = "Prefer"
PREFER_RESPOND_ASYNC = "respond-async"
PREFERENCE_APPLIED_HEADER = "Preference-Applied"
LOCATION_HEADER = "Location"
RETRY_AFTER_HEADER = "Retry-After"
DEPRECATION_HEADER = "Deprecation"
SUNSET_HEADER = "Sunset"
DEPRECATION_ACTIVE = "true"
IDEMPOTENCY_KEY_PATTERN = r"^[\x20-\x7e]+$"  # printable ASCII
IDEMPOTENCY_KEY_REQUIRED_DETAIL = "background mode needs an Idempotency-Key"
IDEMPOTENCY_REUSE_DETAIL = "this Idempotency-Key was used with a different request"
IN_PROGRESS_DETAIL = "the first request with this Idempotency-Key is still running; retry shortly"
ABANDONED_DETAIL = "the first request with this Idempotency-Key did not finish; send a new key to run it again"
IN_PROGRESS_POLL_SECONDS = 0.2
IN_PROGRESS_RETRY_AFTER_SECONDS = 1
PREDICT_PATH = "/v1/predict"
# The status a stored failure is replayed with: the failures that reached Serve (the only ones stored).
STORED_ERROR_STATUS = {
    ProblemCode.MODEL_REJECTED_INPUT: HTTPStatus.UNPROCESSABLE_ENTITY,
    ProblemCode.UPSTREAM_ERROR: HTTPStatus.BAD_GATEWAY,
    ProblemCode.UPSTREAM_TIMEOUT: HTTPStatus.GATEWAY_TIMEOUT,
}
# The prediction worker (planning/24).
RETRY_BACKOFF_SECONDS = (5, 30, 120)  # wait before the 2nd, 3rd, ... try of a background prediction; the last repeats
WORKER_SWEEP_MAX_ROUNDS = 10  # claim-and-run rounds one poll makes before it yields to the next tick, so no tenant's backlog holds the leader forever
LEASE_LOST_DETAIL = "the worker running this prediction stopped answering"
DEPLOYMENT_GONE_DETAIL = "the model's deployment is no longer active"
PREDICTION_RETENTION_PURGE_LOG = "purged %s stored predictions"
CANCEL_CHECK_SECONDS = 2  # how often a worker waiting on Serve asks whether the key cancelled the prediction
SSE_POLL_SECONDS = 1  # GET /v1/predict/{id}/events looks for new events this often (LISTEN/NOTIFY would replace the poll)
SSE_HEARTBEAT_SECONDS = 15  # a quiet event stream sends a comment this often so proxies keep it open
SSE_MEDIA_TYPE = "text/event-stream"
LAST_EVENT_ID_HEADER = "Last-Event-ID"
