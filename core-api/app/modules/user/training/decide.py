"""What a training run becomes, decided without touching anything: no I/O, no connection, no gateway.

A run ends with the job's report.json (docs/trainer-plan.md T2): Ray's state says the job ended, the report says
how. Success needs both.
"""
from pydantic import BaseModel, ConfigDict

from app.integrations.compute import JobStatus
from app.integrations.data_plane.train_report import FailureCode, TrainOutcome, TrainReport
from app.integrations.job_lifecycle import Verdict
from app.modules.user.training.constants import TRANSITIONS, TrainingStatus

RAY_TO_STATUS = {
    JobStatus.PENDING: TrainingStatus.SUBMITTED,
    JobStatus.RUNNING: TrainingStatus.RUNNING,
    JobStatus.SUCCEEDED: TrainingStatus.SUCCEEDED,
    JobStatus.FAILED: TrainingStatus.FAILED,
    JobStatus.STOPPED: TrainingStatus.CANCELLED,
}
FINISHED = (TrainingStatus.SUCCEEDED, TrainingStatus.FAILED)  # the job ended on its own, so it wrote (or should have) a report


class Outcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: TrainingStatus
    report: TrainReport | None = None  # only a run that ended on its own, or was timed out, has one


def failed_report(code: FailureCode, message: str) -> TrainReport:
    return TrainReport(status=TrainOutcome.FAILED, code=code, message=message)


def target(row: dict, verdict: Verdict) -> TrainingStatus | None:
    """The status to move the run to, or None when there is nothing to do (Ray has not moved it, or the move is not
    an edge, e.g. running -> submitted)."""
    if verdict.timed_out:
        return TrainingStatus.FAILED
    if verdict.ray is None:
        return None
    to = RAY_TO_STATUS[verdict.ray]
    return to if to in TRANSITIONS[TrainingStatus(row["status"])] else None


def needs_report(to: TrainingStatus, timed_out: bool) -> bool:
    return to in FINISHED and not timed_out


def resolve(to: TrainingStatus, report: TrainReport | None, timed_out: bool) -> Outcome:
    """Success needs Ray's SUCCEEDED and a report that says so; a job that ended without a usable report has failed,
    whatever Ray says."""
    if timed_out:
        return Outcome(status=TrainingStatus.FAILED, report=failed_report(FailureCode.TIMEOUT, "the training job did not finish in time"))
    if to not in FINISHED:
        return Outcome(status=to)
    report = report or failed_report(FailureCode.INTERNAL, "the training job ended without a report")
    ok = to is TrainingStatus.SUCCEEDED and report.status is TrainOutcome.SUCCEEDED  # Ray's SUCCEEDED only says the driver exited
    return Outcome(status=TrainingStatus.SUCCEEDED if ok else TrainingStatus.FAILED, report=report)
