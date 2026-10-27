"""Training routes (job.read / job.create / job.cancel through the real router, database faked), and the lifecycle
the reconciler drives: a run ends with the job's report, success needs Ray and the report to agree."""
import asyncio
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.core.authz import Role, TenantRole
from app.core.enums import TRAINABLE_TASK_TYPES, TaskType, TrainPreset
from app.core.problem import Problem
from app.core.settings import settings
from app.integrations.compute import JobStatus
from app.integrations.data_plane.train_report import FailureCode, TrainOutcome, TrainReport
from app.integrations.job_spec import TrainData
from app.modules.user.training import create_service, routes, run_service, service, train_spec
from app.modules.user.training.constants import BASE_MODELS, DEFAULT_MODEL_NAME, TRANSITIONS, ProblemCode, TrainingStatus
from tests.support import PROJECT, FakeConn, access, make_client, patch_tenant_conn

BASE = f"/v1/projects/{PROJECT}/training-runs"
RUN = uuid.uuid4()
VERSION, DATASET = uuid.uuid4(), uuid.uuid4()
BODY = {"dataset_version_id": str(VERSION), "preset": TrainPreset.SMALL}
HEADERS = {"Idempotency-Key": "k"}
MIGRATIONS = Path(__file__).parents[4] / "migrations"


def run_row(**over):
    return {
        "id": RUN, "status": "submitted", "dataset_ref": str(VERSION), "dataset_version_id": VERSION, "preset": TrainPreset.SMALL,
        "ray_job_id": f"train-{RUN}", "mlflow_run_id": "mlflow-1", "config": {"epochs": 30, "batch_size": 16, "image_size": 224},
        "metrics": None, "created_at": datetime.now(UTC), "completed_at": None, "tenant_id": uuid.uuid4(), "project_id": PROJECT,
        "task": TaskType.IMAGE_CLASSIFICATION, "report": None, "idempotency_key": "k", "model_artifact": None, "dataset_version": "c0ffee",
        "test_dataset_id": None, "test_dataset_version": None, "reproduction_group_id": None, "dataset_id": DATASET,
        "spec": {"payload": {"seed": 42}}, **over,
    }


# ---- routes ----

@pytest.fixture
def call(monkeypatch, dev_headers):
    client = make_client(routes.router)

    def go(method, path, row, **kw):
        patch_tenant_conn(monkeypatch, FakeConn(row, row=row), run_service)
        return client.request(method, path, **kw)

    return go


@pytest.fixture
def started(monkeypatch):
    """create_run reaches its end: the route answers with whatever create_run returns."""
    async def create_run(ctx, project_id, body, key):
        return run_row()

    monkeypatch.setattr(create_service, "create_run", create_run)


def test_viewer_lists_runs(call):
    assert call("GET", BASE, access()).status_code == 200


@pytest.mark.parametrize(
    "method,path,kw,action",
    [
        ("POST", BASE, {"json": BODY, "headers": HEADERS}, "job.create"),
        ("POST", f"{BASE}/{RUN}/cancel", {}, "job.cancel"),
    ],
)
def test_viewer_denied_403_names_action(call, method, path, kw, action):
    r = call(method, path, access(), **kw)
    assert r.status_code == 403 and r.json()["action"] == action


def test_roles_that_may_create(call, started):
    kw = {"json": BODY, "headers": HEADERS}
    assert call("POST", BASE, access(role=TenantRole.ADMIN), **kw).status_code == 202
    assert call("POST", BASE, access(visibility="private", roles=[Role.MODEL_EDITOR]), **kw).status_code == 202
    assert call("POST", BASE, access(visibility="private", roles=[Role.DEPLOYER]), **kw).status_code == 403


def test_private_unbound_is_404(call):
    assert call("GET", BASE, access(visibility="private")).status_code == 404
    assert call("POST", BASE, access(visibility="private"), json=BODY, headers=HEADERS).status_code == 404


def test_cross_tenant_project_is_404(call):
    gone = access(visibility=None, default_access=None, roles=None)
    assert call("GET", f"{BASE}/{RUN}", gone).status_code == 404
    assert call("POST", BASE, gone, json=BODY, headers=HEADERS).status_code == 404


@pytest.mark.parametrize("body,headers", [
    ({**BODY, "preset": "huge"}, HEADERS),
    ({**BODY, "hyperparameters": {"epochs": 0}}, HEADERS),
    ({**BODY, "dataset_ref": "x"}, HEADERS),  # the old body is gone
    ({"preset": "small"}, HEADERS),
    (BODY, {}),  # no Idempotency-Key
])
def test_a_bad_create_request_is_422(call, started, body, headers):
    assert call("POST", BASE, access(role=TenantRole.ADMIN), json=body, headers=headers).status_code == 422


def test_logs_are_the_raw_tail_only(call, monkeypatch):
    async def logs(job_id):
        return "x" * 10_000

    monkeypatch.setattr(routes.compute, "logs", logs)
    r = call("GET", f"{BASE}/{RUN}/logs", {**access(), "ray_job_id": "j", "status": "running"})
    assert r.status_code == 200 and set(r.json()) == {"status", "logs"} and len(r.json()["logs"]) == 8000


# ---- create ----

class Gateway:
    """The calls create_run makes outside Postgres, recorded."""

    def __init__(self, monkeypatch, fail_submit=False):
        self.spec = self.stopped = self.finished = None
        self.started = []

        async def start_run(*ids):
            self.started.append(ids)
            return SimpleNamespace(run_id="mlflow-1")

        async def prepare(tenant_id, project_id, dataset_id, version_id, run_id, ttl):
            self.ttl = ttl
            return TrainData(index_url="http://i", manifest_url="http://m", version_id=version_id, lakefs_commit="c0ffee")

        async def submit(tenant_id, project_id, run_id, spec):
            self.spec = spec
            if fail_submit:
                raise RuntimeError("ray is down")
            return spec.job_id

        async def stop(job_id):
            self.stopped = job_id

        async def finish_run(*a, **k):
            self.finished = a

        monkeypatch.setattr(create_service.mlflow, "start_run", start_run)
        monkeypatch.setattr(create_service.mlflow, "finish_run", finish_run)
        monkeypatch.setattr(train_spec.data_plane, "prepare_train_data", prepare)
        monkeypatch.setattr(create_service.data_plane, "submit_train", submit)
        monkeypatch.setattr(create_service.compute, "stop", stop)


def make_conn(monkeypatch, *, task=TaskType.IMAGE_CLASSIFICATION, inflight=0, existing=None, version=True):
    conn = FakeConn(
        rows={"idempotency_key = $2": existing, "FROM dataset_versions": {"dataset_id": DATASET, "lakefs_commit": "c0ffee"} if version else None,
              "INSERT INTO training_runs": run_row()},
        vals={"task_type": task, "count(*)": inflight},
    )
    patch_tenant_conn(monkeypatch, conn, create_service)
    return conn


def ctx():
    return SimpleNamespace(tenant_id=str(uuid.uuid4()))


def create(body=BODY, key="k"):
    from app.modules.user.training.schemas import TrainingRunCreate

    return asyncio.run(create_service.create_run(ctx(), PROJECT, TrainingRunCreate.model_validate(body), key))


def test_create_builds_the_spec_stores_it_and_submits_it(monkeypatch):
    gw = Gateway(monkeypatch)
    conn = make_conn(monkeypatch)
    create()
    spec = gw.spec
    assert (spec.kind, spec.job_id.startswith("train-"), spec.resources.gpu) == ("train", True, 1)
    p = spec.payload
    assert (p.task, p.preset, p.base_model) == (TaskType.IMAGE_CLASSIFICATION, TrainPreset.SMALL, BASE_MODELS[(TaskType.IMAGE_CLASSIFICATION, TrainPreset.SMALL)])
    assert (p.hyperparameters.epochs, p.seed, p.data.lakefs_commit) == (30, 42, "c0ffee")
    assert (spec.resources.cpu, spec.resources.max_runtime_s) == (settings.training.task_cpus, settings.training.max_runtime_seconds)
    assert gw.ttl >= settings.training.max_runtime_seconds * settings.ray.max_concurrent_training_runs  # outlasts the queue and the run
    insert = next(args for sql, args in conn.calls if sql.startswith("INSERT INTO training_runs"))
    assert insert[4] == spec.job_id and insert[5] == "mlflow-1" and insert[8] == DATASET and insert[10] == VERSION  # job id, MLflow run, dataset, version
    assert insert[13]["payload"]["data"]["lakefs_commit"] == "c0ffee"  # the spec is stored as submitted


def test_hyperparameters_override_only_what_they_name(monkeypatch):
    gw = Gateway(monkeypatch)
    make_conn(monkeypatch)
    create({**BODY, "hyperparameters": {"epochs": 5}, "seed": 7})
    assert (gw.spec.payload.hyperparameters.epochs, gw.spec.payload.hyperparameters.batch_size, gw.spec.payload.seed) == (5, 16, 7)


def test_a_version_of_another_project_or_tenant_is_404_and_nothing_starts(monkeypatch):
    gw = Gateway(monkeypatch)
    make_conn(monkeypatch, version=False)
    with pytest.raises(Problem) as err:
        create()
    assert err.value.status_code == 404 and not gw.started and gw.spec is None


@pytest.mark.parametrize("task,body,code", [
    (None, BODY, ProblemCode.TASK_TYPE_MISSING),
    (TaskType.TABULAR_REGRESSION, BODY, ProblemCode.TASK_NOT_TRAINABLE),
    (TaskType.TEXT_GENERATION, BODY, "task_type_not_supported"),
    (TaskType.OBJECT_DETECTION, {**BODY, "task_type": TaskType.IMAGE_CLASSIFICATION}, ProblemCode.TASK_TYPE_MISMATCH),
])
def test_the_projects_task_decides_what_can_train(monkeypatch, task, body, code):
    gw = Gateway(monkeypatch)
    make_conn(monkeypatch, task=task)
    with pytest.raises(Problem) as err:
        create(body)
    assert (err.value.status_code, err.value.code) == (422, code) and not gw.started


def test_too_many_runs_in_progress_is_429(monkeypatch):
    gw = Gateway(monkeypatch)
    make_conn(monkeypatch, inflight=settings.ray.max_concurrent_training_runs)
    with pytest.raises(Problem) as err:
        create()
    assert err.value.status_code == 429 and not gw.started


def test_the_same_key_and_request_returns_the_same_run_without_starting_another(monkeypatch):
    gw = Gateway(monkeypatch)
    make_conn(monkeypatch, existing=run_row())
    assert create().id == RUN and not gw.started and gw.spec is None


def test_the_same_key_with_another_request_is_409(monkeypatch):
    Gateway(monkeypatch)
    make_conn(monkeypatch, existing=run_row(preset=TrainPreset.LARGE))
    with pytest.raises(Problem) as err:
        create()
    assert (err.value.status_code, err.value.code) == (409, ProblemCode.IDEMPOTENCY_MISMATCH)


def test_losing_a_race_on_the_key_returns_the_winner_and_submits_nothing(monkeypatch):
    gw = Gateway(monkeypatch)
    conn_rows = {}

    # the insert answers None; the follow-up lookup of the key finds the winner
    class Racing(FakeConn):
        async def fetchrow(self, sql, *args):
            self.calls.append((sql, args))
            if "idempotency_key = $2" in sql:
                return run_row() if conn_rows.get("inserted") else None
            if "INSERT INTO training_runs" in sql:
                conn_rows["inserted"] = True
                return None
            return {"dataset_id": DATASET, "lakefs_commit": "c0ffee"}

    patch_tenant_conn(monkeypatch, Racing(vals={"task_type": TaskType.IMAGE_CLASSIFICATION, "count(*)": 0}), create_service)
    assert create().id == RUN and gw.spec is None


def test_a_submit_that_fails_stops_the_job_and_fails_the_run_with_a_503(monkeypatch):
    gw = Gateway(monkeypatch, fail_submit=True)
    conn = make_conn(monkeypatch)
    with pytest.raises(Problem) as err:
        create()
    assert (err.value.status_code, err.value.code) == (503, ProblemCode.START_FAILED)
    assert gw.stopped == gw.spec.job_id and gw.finished is not None
    failed = next(args for sql, args in conn.calls if sql.startswith("UPDATE training_runs SET status"))
    assert failed[1] == TrainingStatus.FAILED and failed[3]["code"] == FailureCode.INTERNAL


# ---- lifecycle ----

def test_a_finished_run_cannot_move_again():
    assert TrainingStatus.RUNNING not in TRANSITIONS[TrainingStatus.SUCCEEDED]
    assert TrainingStatus.SUBMITTED not in TRANSITIONS[TrainingStatus.RUNNING]
    assert all(not TRANSITIONS[s] for s in (TrainingStatus.SUCCEEDED, TrainingStatus.FAILED, TrainingStatus.CANCELLED))


class _Conn:
    """Answers the row lock with the run, `fetchval` with the model-already-registered check, and records the UPDATE's arguments."""

    def __init__(self, row, lose_the_race=False, has_model=None):
        self.row, self.update_args, self.lose, self.has_model = row, None, lose_the_race, has_model

    async def fetchrow(self, sql, *args):
        if "FOR UPDATE" in sql:
            return self.row
        self.update_args = args
        if self.lose:
            return None  # another writer moved the row between the lock and the UPDATE
        return {**self.row, "status": args[1]}

    async def fetchval(self, sql, *args):
        return self.has_model


def _row(status):
    return {"id": RUN, "status": status, "project_id": PROJECT, "tenant_id": uuid.uuid4(), "mlflow_run_id": "mlflow-1",
            "ray_job_id": f"train-{RUN}", "created_at": datetime.now(UTC)}


class Settle:
    """The calls transition makes outside Postgres, recorded."""

    def __init__(self, monkeypatch, report):
        self.mlflow, self.published, self.registered, self.usage = [], [], [], []

        async def read_train_report(*ids):
            if isinstance(report, Exception):
                raise report
            return report

        async def finish_run(tenant_id, project_id, run_id, status, metrics=None):
            self.mlflow.append((status, metrics))

        async def publish_run_model(*ids):
            self.published.append(ids)

        async def register_version(conn, tenant_id, project_id, run_id, mlflow_run_id, name):
            self.registered.append((run_id, mlflow_run_id, name))

        async def record_usage(conn, **kw):
            self.usage.append(kw)

        monkeypatch.setattr(service.data_plane, "read_train_report", read_train_report)
        monkeypatch.setattr(service.mlflow, "finish_run", finish_run)
        monkeypatch.setattr(service.mlflow, "publish_run_model", publish_run_model)
        monkeypatch.setattr(service, "register_version", register_version)
        monkeypatch.setattr(service, "record_usage", record_usage)


def transition(conn, to=TrainingStatus.SUCCEEDED):
    return asyncio.run(service.transition(conn, dict(conn.row), to, "t1"))


def test_a_cancelled_run_gets_a_completed_at(monkeypatch):
    Settle(monkeypatch, None)

    async def finish_run(*a, **k):
        return None

    monkeypatch.setattr(service.mlflow, "finish_run", finish_run)
    conn = _Conn(_row("running"))
    transition(conn, TrainingStatus.CANCELLED)
    assert conn.update_args[3] is not None  # completed_at
    assert conn.update_args[5] is None  # no report: the job did not end on its own


def test_a_succeeded_job_with_a_good_report_logs_metrics_registers_the_model_and_bills(monkeypatch):
    report = TrainReport(status=TrainOutcome.SUCCEEDED, metrics={"top1": 0.9}, history=[{"epoch": 1, "loss": 0.5}])
    fx = Settle(monkeypatch, report)
    conn = _Conn(_row("running"))
    transition(conn)
    assert fx.mlflow == [("FINISHED", {"top1": 0.9})]
    assert len(fx.published) == 1 and fx.registered == [(RUN, "mlflow-1", DEFAULT_MODEL_NAME)]
    assert fx.usage[0]["source_ref"] == f"train-{RUN}" and fx.usage[0]["unit"] == "gpu_seconds"  # keyed on the job: a second settle bills nothing
    assert conn.update_args[1] == TrainingStatus.SUCCEEDED and conn.update_args[4] == {"top1": 0.9}
    assert conn.update_args[5]["history"] == [{"epoch": 1, "loss": 0.5}]  # the whole report is kept on the row


def _unreadable():
    try:
        TrainReport.model_validate({"status": "failed"})  # a failure without a code
    except ValidationError as e:
        return e


@pytest.mark.parametrize("report", [
    TrainReport(status=TrainOutcome.FAILED, code=FailureCode.OOM, message="cuda oom"),
    None,  # the job ended without writing one
    _unreadable(),  # it wrote one core-api cannot read
], ids=["failed", "missing", "unreadable"])
def test_a_job_that_ends_ok_but_reports_failure_or_nothing_is_failed_and_not_billed(monkeypatch, report):
    fx = Settle(monkeypatch, report)
    conn = _Conn(_row("running"))
    transition(conn)  # Ray said SUCCEEDED
    assert conn.update_args[1] == TrainingStatus.FAILED
    assert fx.mlflow[0][0] == "FAILED" and not fx.published and not fx.registered and not fx.usage
    assert conn.update_args[5]["status"] == "failed"
    if isinstance(report, TrainReport):
        assert conn.update_args[5]["code"] == "oom"
    else:
        assert conn.update_args[5]["code"] == FailureCode.INTERNAL


def test_a_report_that_says_succeeded_does_not_save_a_job_ray_failed(monkeypatch):
    fx = Settle(monkeypatch, TrainReport(status=TrainOutcome.SUCCEEDED))
    conn = _Conn(_row("running"))
    transition(conn, TrainingStatus.FAILED)
    assert conn.update_args[1] == TrainingStatus.FAILED and not fx.registered and not fx.usage


def test_a_second_settle_of_the_same_run_does_nothing(monkeypatch):
    fx = Settle(monkeypatch, TrainReport(status=TrainOutcome.SUCCEEDED))
    conn = _Conn(_row("succeeded"))  # someone else already moved it
    asyncio.run(service.transition(conn, {**conn.row, "status": "running"}, TrainingStatus.SUCCEEDED, "t1"))
    assert conn.update_args is None and not fx.mlflow and not fx.registered and not fx.usage


def test_a_retry_after_a_rollback_does_not_register_the_model_twice(monkeypatch):
    fx = Settle(monkeypatch, TrainReport(status=TrainOutcome.SUCCEEDED))
    transition(_Conn(_row("running"), has_model=1))
    assert not fx.published and not fx.registered


def _ray(monkeypatch, status):
    async def poll(job_id):
        return status

    monkeypatch.setattr(service.job_follower, "poll", poll)


def test_the_sweep_ignores_a_run_someone_else_just_moved(monkeypatch):
    Settle(monkeypatch, TrainReport(status=TrainOutcome.SUCCEEDED))
    _ray(monkeypatch, JobStatus.SUCCEEDED)
    conn = _Conn(_row("running"), lose_the_race=True)
    asyncio.run(service._settle(conn, {**conn.row, "status": TrainingStatus.RUNNING}))  # no exception: the 409 is the expected race


def test_a_job_ray_no_longer_knows_fails_only_past_the_deadline(monkeypatch):
    Settle(monkeypatch, None)
    _ray(monkeypatch, None)
    fresh = _Conn(_row("submitted"))
    asyncio.run(service._settle(fresh, fresh.row))
    assert fresh.update_args is None
    old = _Conn({**_row("submitted"), "created_at": datetime.now(UTC) - timedelta(seconds=train_spec.horizon_seconds() + 60)})
    asyncio.run(service._settle(old, old.row))
    assert old.update_args[1] == TrainingStatus.FAILED


# ---- the enums and the database agree ----

def test_the_status_check_mirrors_the_enum():
    sql = (MIGRATIONS / "20261017172706_init.sql").read_text()
    clause = re.search(r"status\s+text NOT NULL DEFAULT 'submitted'\s+CHECK \(status IN \(([^)]*)\)\)", sql)
    assert set(re.findall(r"'([^']+)'", clause.group(1))) == {s.value for s in TrainingStatus}


def test_every_trainable_task_has_a_base_model_per_preset():
    assert set(BASE_MODELS) == {(t, p) for t in TRAINABLE_TASK_TYPES for p in TrainPreset}
    assert all(m.revision != "main" and len(m.revision) == 40 for m in BASE_MODELS.values())  # pinned to a commit
