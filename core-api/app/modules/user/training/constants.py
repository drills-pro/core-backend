from enum import StrEnum

from app.core.enums import TaskType, TrainPreset
from app.integrations.job_spec import TrainBaseModel

IDEMPOTENCY_HEADER = "Idempotency-Key"

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


START_FAILED_DETAIL = "could not start the training run, try again shortly"
DEFAULT_MODEL_NAME = "model"  # the registered model every succeeded run adds a version to (matches model_registry's MODEL_NAME)
TRAIN_GPUS = 1  # a whole card per run (docs/trainer-plan.md "Ray execution")
SEED_MAX = 2**32 - 1
TRAINING_LOCK_PREFIX = "training:"  # advisory-lock key prefix: serialises the quota check and insert per tenant
JOB_ID_PREFIX = "train-"  # Ray submission id = prefix + run id: known before the POST, so a lost answer can still be stopped
LOST_JOB_GRACE_SECONDS = 3600  # past the run's queue + runtime horizon, a job Ray no longer answers for is failed
MAX_PRESIGN_SECONDS = 7 * 24 * 3600  # SigV4 presigned URLs live at most 7 days

# One pretrained checkpoint per (task, preset), pinned to a commit (docs/trainer-plan.md T6).
BASE_MODELS: dict[tuple[TaskType, TrainPreset], TrainBaseModel] = {
    (TaskType.IMAGE_CLASSIFICATION, TrainPreset.SMALL): TrainBaseModel(repo_id="facebook/convnext-tiny-224", revision="6166b7613034066690a621d8bf25ffdf181a34f0"),
    (TaskType.IMAGE_CLASSIFICATION, TrainPreset.MEDIUM): TrainBaseModel(repo_id="facebook/convnext-small-224", revision="e0df91dcc78dbc0f56be8fbecff706128aee7c28"),
    (TaskType.IMAGE_CLASSIFICATION, TrainPreset.LARGE): TrainBaseModel(repo_id="facebook/convnext-base-224", revision="8550c1da008c99b90c9c42f1d4e1cb650d7a8c05"),
    (TaskType.OBJECT_DETECTION, TrainPreset.SMALL): TrainBaseModel(repo_id="PekingU/rtdetr_v2_r18vd", revision="5650961749fa93567c0d46fc7f43ea4f9e914107"),
    (TaskType.OBJECT_DETECTION, TrainPreset.MEDIUM): TrainBaseModel(repo_id="PekingU/rtdetr_v2_r34vd", revision="5d60d8fb5eb2a3ee5575bf200ecbce4e2cfa420c"),
    (TaskType.OBJECT_DETECTION, TrainPreset.LARGE): TrainBaseModel(repo_id="PekingU/rtdetr_v2_r50vd", revision="282494075698cab9faa1096ae26856890030c817"),
}


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
