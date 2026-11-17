"""A customer's view of a training run: a few named steps and a failure code, never the job's raw output."""
from app.modules.user.training.constants import FailureCode, StepKey, StepState, TrainingStatus
from app.modules.user.training.schemas import RunProgress, RunStep

_FINISHED = {
    TrainingStatus.SUCCEEDED: StepState.DONE,
    TrainingStatus.FAILED: StepState.FAILED,
    TrainingStatus.CANCELLED: StepState.CANCELLED,
}


def failure_of(code: str | None) -> FailureCode:
    try:
        return FailureCode(code)
    except ValueError:
        return FailureCode.INTERNAL


def _training_state(status: str, progress: RunProgress | None) -> StepState:
    if status == TrainingStatus.SUBMITTED:
        return StepState.PENDING
    if status == TrainingStatus.RUNNING:
        return StepState.ACTIVE
    if status == TrainingStatus.SUCCEEDED:
        return StepState.DONE
    # a run that ended before its first epoch never trained: its training step is the one that failed or was cancelled
    if progress is not None:
        return StepState.DONE
    return _FINISHED.get(status, StepState.PENDING)


def steps_of(status: str, progress: RunProgress | None, code: str | None) -> list[RunStep]:
    latest = progress.latest if progress else None
    return [
        RunStep(key=StepKey.QUEUED, state=StepState.ACTIVE if status == TrainingStatus.SUBMITTED else StepState.DONE),
        RunStep(
            key=StepKey.TRAINING, state=_training_state(status, progress),
            epoch=latest.epoch if latest else None, epochs=latest.epochs if latest else None),
        RunStep(
            key=StepKey.FINISHED, state=_FINISHED.get(status, StepState.PENDING),
            failure=failure_of(code) if status == TrainingStatus.FAILED else None),
    ]
