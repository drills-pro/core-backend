"""Named values for deployments and inference."""
from enum import StrEnum
from http import HTTPStatus

from app.core.enums import PlanTier, TaskType
from app.integrations.serve_client import ServeErrorCode


class ProblemCode(StrEnum):
    BODY_TOO_LARGE = "body_too_large"
    BODY_NOT_JSON = "body_not_json"
    BODY_NOT_OBJECT = "body_not_object"
    NOT_DEPLOYABLE = "not_deployable"
    NOT_ACTIVE = "deployment_not_active"
    PRECONDITION_FAILED = "precondition_failed"
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
    NOT_ACCEPTABLE = "not_acceptable"
    DEPLOY_RISK = "deploy_risk_unacknowledged"


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
    CREATE = "create"
    REDEPLOY = "redeploy"
    SET_MAIN = "set_main"
    STOP = "stop"
    START = "start"
    DELETE = "delete"


class EntityType(StrEnum):
    DEPLOYMENT = "deployment"


# req/04 FR4: rate limit "configurable per tenant plan tier."
PLAN_TIER_RATE_LIMITS = {PlanTier.FREE: 10, PlanTier.PRO: 120, PlanTier.ENTERPRISE: 1000, PlanTier.INTERNAL: 1000}

DEFAULT_COMPUTE_TARGET = "cpu"
SETTING_COMPUTE_TARGET = "compute_target"
METADATA_FIELD = "metadata"
PREDICTIONS_FIELD = "predictions"
TEST_CALLS_DEFAULT_LIMIT = 20
TEST_CALLS_MIN_LIMIT = 1
TEST_CALLS_MAX_LIMIT = 100
DEPLOYMENT_LOCK_PREFIX = "deployments:"  # advisory-lock key prefix: serialises the quota check and the write per tenant
DEPLOYMENT_EXISTS_DETAIL = "this model version already has a deployment; use it (start it if it is stopped) or delete it first"
TOO_MANY_DEPLOYMENTS_DETAIL = "too many active deployments for this plan, stop or delete one first"
PRECONDITION_FAILED_DETAIL = "the deployment changed since you read it; fetch it again and retry with its new ETag"
STOPPED_DETAIL = "the deployment is stopped; start it to serve again"
TOO_MANY_REQUESTS_DETAIL = "too many concurrent requests for this deployment"
TARGET_CACHE_SIZE = 1024  # (deployment, run) pairs whose model URLs core-api remembers

DEPLOY_RISK_DETAIL = "this run has no test dataset, so its metrics were not measured on unseen data; deploy again with acknowledge_risk=true to accept that"
DEPLOY_BELOW_FLOOR_DETAIL = "this run's {metric} is {value:.2f}, under the {floor:.2f} floor for the task; deploy again with acknowledge_risk=true to accept that"
# The score a run should reach before it is served without a warning: (metric the trainer reports, floor). A task not listed has no floor.
METRIC_FLOORS: dict[TaskType, tuple[str, float]] = {
    TaskType.IMAGE_CLASSIFICATION: ("top1", 0.5),
    TaskType.OBJECT_DETECTION: ("map50", 0.3),
}
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
ETAG_HEADER = "ETag"
IF_MATCH_HEADER = "If-Match"
DEPRECATION_ACTIVE = "true"
IDEMPOTENCY_KEY_PATTERN = r"^[\x20-\x7e]+$"  # printable ASCII
IDEMPOTENCY_KEY_REQUIRED_DETAIL = "background mode needs an Idempotency-Key"
IDEMPOTENCY_REUSE_DETAIL = "this Idempotency-Key was used with a different request"
IN_PROGRESS_DETAIL = "the first request with this Idempotency-Key is still running; retry shortly"
ABANDONED_DETAIL = "the first request with this Idempotency-Key did not finish; send a new key to run it again"
IN_PROGRESS_POLL_SECONDS = 0.2
IN_PROGRESS_RETRY_AFTER_SECONDS = 1
COLD_START_RETRY_AFTER_SECONDS = 5  # a timed-out call is usually a replica still loading the model: tell the caller when to retry
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
ACCEPT_HEADER = "Accept"
JSON_ACCEPTABLE = ("application/json", "application/*", "*/*")  # an Accept entry that lets the sync JSON answer through
NOT_STREAMABLE_DETAIL = "no model can stream yet (no serve adapter declares streaming); send Accept: application/json, or Prefer: respond-async"
LAST_EVENT_ID_HEADER = "Last-Event-ID"


# What a deployment's callers send and get back, by what the task type's encoder and postprocess functions read and write
# (ray_serving/inputs.py, ray_serving/postprocess.py); the keys are those function names.
INPUT_FIELD_BY_ENCODER = {"image_encoder": "image_base64", "text_encoder": "text"}
TABULAR_ENCODER = "tabular_encoder"  # its body is one value per feature column of the model, not one field
FEATURE_TYPE = "number"
NUMBER_PLACEHOLDER = "<number>"
CATEGORY_TYPE = "string"  # a feature column the trainer encoded from text (report.categories): the caller sends the text
CATEGORY_PLACEHOLDER = "<text>"
OUTPUT_ITEM_BY_POSTPROCESS = {
    "regress": {"value": "number"},
    "classify": {"class": "string", "probability": "number"},
    "ranked_probabilities": {"class": "string", "probability": "number"},
    "detect": {"class": "string", "probability": "number", "box": {"x": "number", "y": "number", "width": "number", "height": "number"}},
}
SNIPPET_KEY_PLACEHOLDER = "<YOUR_API_KEY>"
SNIPPET_INPUT_PLACEHOLDER = "<BASE64_IMAGE>"
