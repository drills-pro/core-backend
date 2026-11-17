"""A customer's steps: named, from the row and its reported epochs, with a closed failure code and no report message."""
from datetime import UTC, datetime

from app.modules.user.training.constants import FailureCode, StepKey, StepState, TrainingStatus as S
from app.modules.user.training.schemas import ProgressPoint, RunProgress, TrainingRun
from app.modules.user.training.steps import failure_of, steps_of

NOW = datetime(2026, 11, 17, tzinfo=UTC)


def progress(epoch, epochs):
    point = ProgressPoint(seq=epoch, epoch=epoch, epochs=epochs, metrics={"top1": 0.5}, created_at=NOW)
    return RunProgress(latest=point, history=[point])


def states(steps):
    return [(s.key, s.state) for s in steps]


def test_a_queued_run_is_waiting_for_its_job():
    assert states(steps_of(S.SUBMITTED, None, None)) == [
        (StepKey.QUEUED, StepState.ACTIVE), (StepKey.TRAINING, StepState.PENDING), (StepKey.FINISHED, StepState.PENDING)]


def test_a_running_run_shows_its_epoch():
    steps = steps_of(S.RUNNING, progress(3, 30), None)
    assert states(steps)[:2] == [(StepKey.QUEUED, StepState.DONE), (StepKey.TRAINING, StepState.ACTIVE)]
    assert (steps[1].epoch, steps[1].epochs) == (3, 30)


def test_a_succeeded_run_is_done_at_every_step():
    assert [st for _, st in states(steps_of(S.SUCCEEDED, progress(30, 30), None))] == [StepState.DONE] * 3


def test_a_failed_run_names_its_code_and_nothing_more():
    steps = steps_of(S.FAILED, progress(2, 30), "oom")
    assert states(steps)[-1] == (StepKey.FINISHED, StepState.FAILED) and steps[-1].failure == FailureCode.OOM


def test_a_failure_before_any_epoch_leaves_training_failed_not_done():
    steps = steps_of(S.FAILED, None, "bad_data")
    assert states(steps)[1] == (StepKey.TRAINING, StepState.FAILED) and steps[-1].failure == FailureCode.BAD_DATA


def test_an_unknown_code_reads_as_internal_so_no_free_text_leaks():
    assert failure_of("Traceback: /secret/path") is FailureCode.INTERNAL
    assert failure_of(None) is FailureCode.INTERNAL


def test_a_cancelled_run_before_its_first_epoch_is_cancelled():
    assert states(steps_of(S.CANCELLED, None, None))[1:] == [
        (StepKey.TRAINING, StepState.CANCELLED), (StepKey.FINISHED, StepState.CANCELLED)]


def test_the_run_report_drops_its_message_for_every_caller():
    run = TrainingRun.model_validate({**_run_row(), "report": {"code": "internal", "message": "boom https://s3?X-Amz=1"}})
    assert run.report == {"code": "internal"}


def _run_row():
    import uuid

    return {
        "id": uuid.uuid4(), "status": "failed", "dataset_ref": "d", "dataset_version_id": None, "preset": "small", "ray_job_id": "j",
        "mlflow_run_id": None, "config": {}, "metrics": None, "created_at": NOW, "completed_at": None, "tenant_id": uuid.uuid4(),
        "project_id": uuid.uuid4(), "task": None, "idempotency_key": "k", "model_artifact": None, "dataset_version": None,
        "test_dataset_id": None, "test_dataset_version": None, "reproduction_group_id": None, "dataset_id": None, "progress": None,
    }
