from enum import StrEnum

NOT_REGISTRABLE_DETAIL = "training run has no completed MLflow run to register"

class TrainingStatus(StrEnum):
    """Training-run lifecycle. Mirrors the training_runs.status CHECK constraint."""

    SUBMITTED = "submitted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ProblemCode(StrEnum):
    INVALID_STATE = "invalid_training_state"
    TASK_TYPE_MISSING = "project_task_type_missing"
    TASK_TYPE_MISMATCH = "task_type_mismatch"
    TASK_NOT_TRAINABLE = "task_type_not_trainable"
    TOO_MANY_RUNS = "too_many_training_runs"
    IDEMPOTENCY_MISMATCH = "idempotency_mismatch"
    START_FAILED = "training_start_failed"
    UNKNOWN_PRESET = "unknown_preset"
    HYPERPARAMETER_OUT_OF_RANGE = "hyperparameter_out_of_range"
    NOT_REGISTRABLE = "run_not_registrable"


START_FAILED_DETAIL = "could not start the training run, try again shortly"
DEFAULT_MODEL_NAME = "model"  # the registered model every succeeded run adds a version to (matches model_registry's MODEL_NAME)
SEED_MAX = 2**32 - 1
TRAINING_LOCK_PREFIX = "training:"  # advisory-lock key prefix: serialises the quota check and insert per tenant
JOB_ID_PREFIX = "train-"  # Ray submission id = prefix + run id: known before the POST, so a lost answer can still be stopped
LOST_JOB_GRACE_SECONDS = 3600  # past the run's queue + runtime horizon, a job Ray no longer answers for is failed
MAX_PRESIGN_SECONDS = 7 * 24 * 3600  # SigV4 presigned URLs live at most 7 days

S = TrainingStatus
# Legal transitions: every status change is a compare-and-set along one of these edges.
TRANSITIONS: dict[TrainingStatus, frozenset[TrainingStatus]] = {
    S.SUBMITTED: frozenset({S.RUNNING, S.SUCCEEDED, S.FAILED, S.CANCELLED}),  # a fast job is never seen running
    S.RUNNING: frozenset({S.SUCCEEDED, S.FAILED, S.CANCELLED}),
    S.SUCCEEDED: frozenset(),
    S.FAILED: frozenset(),
    S.CANCELLED: frozenset(),
}
INFLIGHT_STATUSES = (S.SUBMITTED, S.RUNNING)  # mirrors training_runs_inflight_idx
CANCELLABLE = INFLIGHT_STATUSES

LOG_TAIL_CHARS = 8000
