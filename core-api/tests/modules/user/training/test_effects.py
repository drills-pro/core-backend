"""effects: the gateway calls of a settling run, with the gateways faked."""
import asyncio
import uuid

import httpx
import pytest
from pydantic import ValidationError

from app.integrations import data_plane
from app.integrations.data_plane.train_report import FailureCode, TrainOutcome, TrainReport
from app.modules.user.training import decide, effects
from app.modules.user.training.constants import TrainingStatus as S

ROW = {"id": uuid.uuid4(), "project_id": uuid.uuid4(), "mlflow_run_id": "mlflow-1", "ray_job_id": "train-1"}
OK = TrainReport(status=TrainOutcome.SUCCEEDED, metrics={"top1": 0.9})


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def mlflow_calls(monkeypatch):
    calls = []

    async def finish_run(tenant_id, project_id, run_id, status, metrics=None):
        calls.append((status, metrics))

    async def publish_run_model(*ids):
        calls.append(("published", ids))

    monkeypatch.setattr(data_plane, "finish_run", finish_run)
    monkeypatch.setattr(data_plane, "publish_run_model", publish_run_model)
    return calls


def _reader(monkeypatch, answer):
    async def read_train_report(*ids):
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(effects.data_plane, "read_train_report", read_train_report)


def test_final_report_is_what_the_job_wrote(monkeypatch):
    _reader(monkeypatch, OK)
    assert run(effects.final_report("t", ROW)) == OK


def test_a_missing_report_is_none(monkeypatch):
    _reader(monkeypatch, None)
    assert run(effects.final_report("t", ROW)) is None


def test_an_unreadable_report_is_a_failed_one(monkeypatch):
    try:
        TrainReport.model_validate({"status": "failed"})
    except ValidationError as e:
        _reader(monkeypatch, e)
    report = run(effects.final_report("t", ROW))
    assert report.status is TrainOutcome.FAILED and report.code is FailureCode.INTERNAL


def test_a_store_that_does_not_answer_raises_so_the_sweep_retries(monkeypatch):
    _reader(monkeypatch, RuntimeError("s3 down"))
    with pytest.raises(RuntimeError):
        run(effects.final_report("t", ROW))


@pytest.mark.parametrize("outcome,expected", [
    (decide.Outcome(status=S.SUCCEEDED, report=OK), ("FINISHED", {"top1": 0.9})),
    (decide.Outcome(status=S.FAILED, report=decide.failed_report(FailureCode.OOM, "x")), ("FAILED", {})),
    (decide.Outcome(status=S.CANCELLED), ("KILLED", None)),
])
def test_close_mlflow_closes_the_run_as_the_outcome_says(mlflow_calls, outcome, expected):
    run(effects.close_mlflow("t", ROW, outcome))
    assert mlflow_calls == [expected]


def test_a_run_still_going_or_without_an_mlflow_run_closes_nothing(mlflow_calls):
    run(effects.close_mlflow("t", ROW, decide.Outcome(status=S.RUNNING)))
    run(effects.close_mlflow("t", {**ROW, "mlflow_run_id": None}, decide.Outcome(status=S.CANCELLED)))
    assert mlflow_calls == []


def test_a_cancel_swallows_a_tracking_error_but_a_finished_run_does_not(monkeypatch):
    async def down(*a, **k):
        raise httpx.ConnectError("mlflow down")

    monkeypatch.setattr(data_plane, "finish_run", down)
    run(effects.close_mlflow("t", ROW, decide.Outcome(status=S.CANCELLED)))
    with pytest.raises(httpx.ConnectError):
        run(effects.close_mlflow("t", ROW, decide.Outcome(status=S.SUCCEEDED, report=OK)))


def test_publish_model_needs_an_mlflow_run(mlflow_calls):
    run(effects.publish_model("t", ROW))
    run(effects.publish_model("t", {**ROW, "mlflow_run_id": None}))
    assert [c[0] for c in mlflow_calls] == ["published"]
