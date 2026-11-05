"""persist: the one write of a settling run. Only the compare-and-set winner registers the model and bills."""
import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.integrations.data_plane.train_report import FailureCode, TrainOutcome, TrainReport
from app.modules.user.training import decide, persist
from app.modules.user.model_registry import RegisteredVersion
from app.modules.user.training.constants import TrainingStatus as S
from tests.support.job_table import FakeJobTable

OK = TrainReport(status=TrainOutcome.SUCCEEDED, metrics={"top1": 0.9})
SUCCEEDED = decide.Outcome(status=S.SUCCEEDED, report=OK)
RESOURCES = {"cpu": 1, "memory_bytes": 1}
REGISTERED = RegisteredVersion(mlflow_model_name="t/p/model", mlflow_version="3")


@pytest.fixture
def env(monkeypatch):
    class Env:
        table = FakeJobTable(persist.TRAINING)
        registered, usage = [], []

        def seed(self, status=S.RUNNING, **kw):
            base = {"project_id": uuid.uuid4(), "mlflow_run_id": "mlflow-1", "ray_job_id": "train-1", "metrics": None, "report": None,
                    "created_at": datetime.now(UTC) - timedelta(seconds=90),
                    "started_at": datetime.now(UTC) - timedelta(seconds=60), "spec": {"resources": {**RESOURCES, "gpu": 0.5}}}
            return self.table.seed(status, **{**base, **kw})

        def apply(self, row, outcome, registered=REGISTERED):
            async def go():
                async with self.table(None) as conn:
                    return await persist.apply(conn, row, outcome, registered)

            return asyncio.run(go())

    e = Env()

    async def record_version(conn, tenant_id, project_id, run_id, registered):
        e.registered.append((tenant_id, project_id, run_id, registered))

    async def record_usage(conn, **kw):
        e.usage.append(kw)

    monkeypatch.setattr(persist, "record_version", record_version)
    monkeypatch.setattr(persist, "record_usage", record_usage)
    return e


def test_success_moves_the_run_stores_metrics_and_report_registers_the_model_and_bills_the_gpu_runtime(env):
    row = env.seed()
    moved = env.apply(dict(row), SUCCEEDED)
    assert moved["status"] == S.SUCCEEDED and moved["metrics"] == {"top1": 0.9} and moved["report"]["status"] == "succeeded"
    assert env.registered == [(str(row["tenant_id"]), row["project_id"], row["id"], REGISTERED)]
    bill = env.usage[0]
    assert bill["occurred_at"] == moved["completed_at"] and bill["source_ref"] == "train-1" and bill["project_id"] == row["project_id"]
    assert bill["quantity"] == pytest.approx((moved["completed_at"] - row["started_at"]).total_seconds() * 0.5)  # run time, not queue time, times the fraction


def test_the_loser_of_the_race_writes_nothing_else(env):
    row = env.seed()
    snapshot = dict(row)
    assert env.apply(snapshot, SUCCEEDED) is not None
    assert env.apply(snapshot, SUCCEEDED) is None  # a second settle of the same read
    assert len(env.registered) == len(env.usage) == 1


def test_a_run_moved_by_someone_else_is_left_alone(env):
    row = env.seed(S.CANCELLED)
    assert env.apply({**row, "status": S.RUNNING}, SUCCEEDED) is None
    assert row["status"] == S.CANCELLED and not env.registered and not env.usage


FAILED = decide.Outcome(status=S.FAILED, report=decide.failed_report(FailureCode.OOM, "oom"))


@pytest.mark.parametrize("outcome", [FAILED, decide.Outcome(status=S.CANCELLED)])
def test_a_failed_or_cancelled_run_that_started_is_billed_but_registers_no_model(env, outcome):
    row = env.seed()
    assert env.apply(dict(row), outcome)["status"] == outcome.status
    assert not env.registered and len(env.usage) == 1 and env.usage[0]["quantity"] > 0


@pytest.mark.parametrize("outcome", [SUCCEEDED, FAILED, decide.Outcome(status=S.CANCELLED)])
def test_a_run_never_seen_running_is_not_billed(env, outcome):
    row = env.seed(S.SUBMITTED, started_at=None)
    assert env.apply(dict(row), outcome)["status"] == outcome.status
    assert not env.usage


def test_a_cpu_run_is_not_billed_gpu_seconds(env):
    row = env.seed(spec={"resources": RESOURCES})
    env.apply(dict(row), SUCCEEDED)
    assert not env.usage


def test_a_whole_gpu_for_a_known_time_bills_that_many_gpu_seconds(env):
    t = datetime(2026, 1, 1, tzinfo=UTC)
    run = {"started_at": t, "completed_at": t + timedelta(seconds=100), "spec": {"resources": {**RESOURCES, "gpu": 2}}}
    assert persist.gpu_seconds(run) == 200


def test_moving_to_running_stamps_started_at_and_bills_nothing(env):
    row = env.seed(S.SUBMITTED, started_at=None)
    moved = env.apply(dict(row), decide.Outcome(status=S.RUNNING))
    assert moved["started_at"] is not None and not env.usage


def test_a_run_without_an_mlflow_run_is_billed_but_records_no_model(env):
    row = env.seed(mlflow_run_id=None)
    env.apply(dict(row), SUCCEEDED, None)
    assert not env.registered and len(env.usage) == 1


def test_running_is_not_an_end_so_it_stamps_no_completion(env):
    row = env.seed(S.SUBMITTED)
    assert env.apply(dict(row), decide.Outcome(status=S.RUNNING))["completed_at"] is None
