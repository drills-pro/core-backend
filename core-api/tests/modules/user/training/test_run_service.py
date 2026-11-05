"""Cancel stops the job and settles the row through service.cancel, holding no connection across either; logs are
read with none held either."""
import asyncio
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.problem import Problem
from app.integrations.data_plane import mlflow
from app.modules.user.training import persist, run_service, service
from app.modules.user.training.constants import ProblemCode, TrainingStatus
from tests.support import PROJECT, FakeConn, patch_tenant_conn
from tests.support.job_table import FakeJobConn, FakeJobTable, patch

CTX = SimpleNamespace(tenant_id=str(uuid.uuid4()))
S = TrainingStatus


def cancel(run_id):
    return asyncio.run(run_service.cancel_run(CTX, PROJECT, run_id))


def test_cancel_rejects_a_finished_run(monkeypatch):
    patch_tenant_conn(monkeypatch, FakeConn(row={"status": "succeeded", "ray_job_id": "job-1"}), run_service)
    with pytest.raises(HTTPException) as e:
        cancel(uuid.uuid4())
    assert e.value.status_code == 409


def test_cancel_of_an_unknown_run_is_404(monkeypatch):
    patch_tenant_conn(monkeypatch, FakeConn(row=None), run_service)
    with pytest.raises(HTTPException) as e:
        cancel(uuid.uuid4())
    assert e.value.status_code == 404


class Env:
    """A run in the table; reads of it by id answer from the table (the SELECT run_service does)."""

    def __init__(self, monkeypatch, status=S.RUNNING):
        self.table = FakeJobTable(persist.TRAINING)
        self.row = self.table.seed(status, project_id=PROJECT, mlflow_run_id="mlflow-1", ray_job_id="train-1",
                                   created_at=datetime.now(UTC), metrics=None, report=None,
                                   dataset_ref="v", dataset_version_id=None, preset="small", config={}, completed_at=None,
                                   task="image-classification", idempotency_key="k", model_artifact=None, dataset_version=None,
                                   test_dataset_id=None, test_dataset_version=None, reproduction_group_id=None, dataset_id=None)
        self.stopped, self.killed, self.open_during, self.metrics = [], [], [], []
        outer = self

        class Conn(FakeJobConn):
            async def fetchrow(self, sql, *args):
                if sql.startswith("SELECT * FROM training_runs"):
                    return dict(outer.row)
                return await super().fetchrow(sql, *args)

        self.table.conn_cls = Conn
        patch(monkeypatch, self.table, run_service, service)

        async def stop(job_id):
            self.open_during.append(self.table.open)
            self.stopped.append(job_id)

        async def finish_run(tenant_id, project_id, run_id, status, metrics=None):
            self.open_during.append(self.table.open)
            self.killed.append(status)

        monkeypatch.setattr(run_service.compute, "stop", stop)
        monkeypatch.setattr(mlflow, "finish_run", finish_run)
        monkeypatch.setattr(service, "usage_metrics", SimpleNamespace(training_runs=SimpleNamespace(add=lambda n, a: self.metrics.append(a["status"]))))


@pytest.mark.parametrize("status", [S.SUBMITTED, S.RUNNING])
def test_cancel_stops_the_job_then_settles_the_row_without_holding_a_connection(monkeypatch, status):
    env = Env(monkeypatch, status)
    out = cancel(env.row["id"])
    assert env.stopped == ["train-1"] and env.killed == [mlflow.RunStatus.KILLED] and env.metrics == [S.CANCELLED]
    assert env.row["status"] == S.CANCELLED and env.row["completed_at"] is not None and out.status == S.CANCELLED
    assert env.open_during == [0, 0]  # not across compute.stop, not across the MLflow call


def test_a_cancel_that_loses_the_race_is_a_409_and_counts_nothing(monkeypatch):
    env = Env(monkeypatch)
    stop = run_service.compute.stop

    async def stop_then_settle_elsewhere(job_id):
        await stop(job_id)
        env.row["status"] = S.SUCCEEDED  # the sweep got there first

    monkeypatch.setattr(run_service.compute, "stop", stop_then_settle_elsewhere)
    with pytest.raises(Problem) as err:
        cancel(env.row["id"])
    assert (err.value.status_code, err.value.code) == (409, ProblemCode.INVALID_STATE) and env.metrics == []


def test_a_cancel_the_sweep_settled_first_is_still_a_success_and_counts_nothing(monkeypatch):
    env = Env(monkeypatch)
    stop = run_service.compute.stop

    async def stop_then_sweep_sees_it_stopped(job_id):
        await stop(job_id)
        env.row["status"] = S.CANCELLED

    monkeypatch.setattr(run_service.compute, "stop", stop_then_sweep_sees_it_stopped)
    assert cancel(env.row["id"]).status == S.CANCELLED and env.metrics == []


def test_a_tracking_hiccup_does_not_block_a_cancel(monkeypatch):
    import httpx

    env = Env(monkeypatch)

    async def down(*a, **k):
        raise httpx.ConnectError("mlflow down")

    monkeypatch.setattr(mlflow, "finish_run", down)
    assert cancel(env.row["id"]).status == S.CANCELLED


def test_logs_are_the_tail_and_read_with_no_connection_held(monkeypatch):
    seen = {}

    async def logs(job_id):
        seen["open"] = table.open
        return "x" * 10_000

    table = FakeJobTable(persist.TRAINING)
    patch_tenant_conn(monkeypatch, FakeConn(row={"ray_job_id": "j", "status": "running"}), run_service)
    monkeypatch.setattr(run_service.compute, "logs", logs)
    out = asyncio.run(run_service.get_logs(CTX, PROJECT, uuid.uuid4()))
    assert (out.status, len(out.logs)) == ("running", run_service.LOG_TAIL_CHARS) and seen["open"] == 0


def _register(monkeypatch, row):
    calls = []

    async def fake_register_run(ctx, project_id, run_id, name, mlflow_run_id):
        calls.append((name, mlflow_run_id))
        return "version"

    patch_tenant_conn(monkeypatch, FakeConn(rows={"FROM training_runs": row}), run_service)
    monkeypatch.setattr(run_service, "register_run", fake_register_run)
    return calls


RUN_ROW = {"id": uuid.uuid4(), "status": S.SUCCEEDED, "mlflow_run_id": "mlflow-1", "class_names": []}


def test_register_hands_the_mlflow_run_to_the_registry(monkeypatch):
    calls = _register(monkeypatch, RUN_ROW)
    assert asyncio.run(run_service.register_model(CTX, PROJECT, RUN_ROW["id"], "clf")) == "version"
    assert calls == [("clf", "mlflow-1")]


@pytest.mark.parametrize("row", [{**RUN_ROW, "status": S.FAILED}, {**RUN_ROW, "status": S.RUNNING}, {**RUN_ROW, "mlflow_run_id": None}])
def test_only_a_succeeded_run_with_an_mlflow_run_registers(monkeypatch, row):
    calls = _register(monkeypatch, row)
    with pytest.raises(HTTPException) as e:
        asyncio.run(run_service.register_model(CTX, PROJECT, RUN_ROW["id"], "clf"))
    assert e.value.status_code == 409 and not calls


def test_register_of_another_projects_run_is_404(monkeypatch):
    calls = _register(monkeypatch, None)
    with pytest.raises(Problem) as e:
        asyncio.run(run_service.register_model(CTX, PROJECT, RUN_ROW["id"], "clf"))
    assert e.value.status_code == 404 and not calls
