from enum import StrEnum


class TrainingStatus(StrEnum):
    """Training-run lifecycle. Mirrors the training_runs.status CHECK constraint."""

    SUBMITTED = "submitted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ProblemCode(StrEnum):
    INVALID_STATE = "invalid_training_state"


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
