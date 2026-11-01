"""decide: what a run becomes, as a table; no fakes because it touches nothing."""
import pytest

from app.integrations.compute import JobStatus
from app.integrations.data_plane.train_report import FailureCode, TrainOutcome, TrainReport
from app.integrations.job_lifecycle import Verdict, phase
from app.modules.user.training import decide
from app.modules.user.training.constants import TrainingStatus as S

OK = TrainReport(status=TrainOutcome.SUCCEEDED, metrics={"top1": 0.9})
BAD = TrainReport(status=TrainOutcome.FAILED, code=FailureCode.OOM, message="cuda oom")


def verdict(ray, timed_out=False):
    return Verdict(ray=ray, phase=phase(ray) if ray else None, timed_out=timed_out)


@pytest.mark.parametrize("status,ray,timed_out,expected", [
    (S.SUBMITTED, JobStatus.PENDING, False, None),  # submitted -> submitted is not an edge
    (S.SUBMITTED, JobStatus.RUNNING, False, S.RUNNING),
    (S.SUBMITTED, JobStatus.SUCCEEDED, False, S.SUCCEEDED),  # a fast job is never seen running
    (S.RUNNING, JobStatus.PENDING, False, None),  # not running -> submitted
    (S.RUNNING, JobStatus.RUNNING, False, None),
    (S.RUNNING, JobStatus.SUCCEEDED, False, S.SUCCEEDED),
    (S.RUNNING, JobStatus.FAILED, False, S.FAILED),
    (S.RUNNING, JobStatus.STOPPED, False, S.CANCELLED),
    (S.RUNNING, None, False, None),  # no answer and not due: nothing is decided
    (S.RUNNING, None, True, S.FAILED),
    (S.SUBMITTED, JobStatus.RUNNING, True, S.FAILED),  # out of time while Ray still says running
])
def test_target(status, ray, timed_out, expected):
    assert decide.target({"status": status}, verdict(ray, timed_out)) == expected


def test_only_a_job_that_ended_on_its_own_has_a_report_to_read():
    assert decide.needs_report(S.SUCCEEDED, False) and decide.needs_report(S.FAILED, False)
    assert not decide.needs_report(S.FAILED, True)  # timed out: the job did not end, there is no report to wait for
    assert not decide.needs_report(S.CANCELLED, False) and not decide.needs_report(S.RUNNING, False)


@pytest.mark.parametrize("to,report,expected_status,expected_code", [
    (S.SUCCEEDED, OK, S.SUCCEEDED, None),
    (S.SUCCEEDED, BAD, S.FAILED, FailureCode.OOM),  # Ray's SUCCEEDED only says the driver exited
    (S.SUCCEEDED, None, S.FAILED, FailureCode.INTERNAL),  # no report
    (S.FAILED, OK, S.FAILED, None),  # a report cannot save a job Ray failed
    (S.FAILED, BAD, S.FAILED, FailureCode.OOM),
    (S.FAILED, None, S.FAILED, FailureCode.INTERNAL),
])
def test_resolve_needs_ray_and_the_report_to_agree_on_success(to, report, expected_status, expected_code):
    out = decide.resolve(to, report, timed_out=False)
    assert out.status == expected_status and out.report.code == expected_code


def test_resolve_of_a_timeout_is_a_timeout_failure_whatever_else_was_known():
    out = decide.resolve(S.FAILED, OK, timed_out=True)
    assert out.status == S.FAILED and out.report.code == FailureCode.TIMEOUT


@pytest.mark.parametrize("to", [S.RUNNING, S.CANCELLED])
def test_resolve_of_a_move_that_is_not_an_end_on_its_own_carries_no_report(to):
    assert decide.resolve(to, None, timed_out=False) == decide.Outcome(status=to, report=None)
