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
from app.core.enums import JobPackageName, TaskType
from app.core.problem import Problem
from app.core.settings import settings
from app.integrations import compute, job_lifecycle
from app.integrations.compute import JobStatus
from app.integrations.data_plane.train_report import FailureCode, TrainOutcome, TrainReport
from app.integrations.job_spec import TrainData
from app.modules.user.model_registry import RegisteredVersion
from app.modules.user.training import create_service, effects, persist, routes, run_service, service, train_spec
from app.modules.user.training.constants import DEFAULT_MODEL_NAME, TRANSITIONS, ProblemCode, TrainingStatus
from tests.support import PROJECT, FakeConn, access, make_client, patch_tenant_conn
from tests.support.presets import LIMITS, preset_row
from tests.support.task_types import seeded
from tests.support.job_table import FakeJobTable, patch

BASE = f"/v1/projects/{PROJECT}/training-runs"
RUN = uuid.uuid4()
PACKAGE_SHA = "b" * 64
BUNDLE_SHA = "f" * 64
VERSION, DATASET = uuid.uuid4(), uuid.uuid4()
BODY = {"dataset_version_id": str(VERSION), "preset": "small"}
HEADERS = {"Idempotency-Key": "k"}
MIGRATIONS = Path(__file__).parents[4] / "migrations"


def run_row(**over):
    return {
        "id": RUN, "status": "submitted", "dataset_ref": str(VERSION), "dataset_version_id": VERSION, "preset": "small",
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
    ({**BODY, "preset": "Not A Name!"}, HEADERS),
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

    monkeypatch.setattr(run_service.compute, "logs", logs)
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

        async def prepare(tenant_id, project_id, dataset_id, version_id, run_id, ttl, *, data_kind=None):
            self.ttl = ttl
            return TrainData(index_url="http://i", manifest_url="http://m", version_id=version_id, lakefs_commit="c0ffee")

        async def resolve_stable(name):
            self.package_name = name
            return SimpleNamespace(sha256=PACKAGE_SHA, name=name)

        async def submit(tenant_id, project_id, run_id, spec, package_sha256, events_token=None, *, bundle_name):
            self.spec, self.package_sha, self.events_token, self.bundle_name = spec, package_sha256, events_token, bundle_name
            if fail_submit:
                raise RuntimeError("ray is down")
            return spec.job_id

        async def stop(job_id):
            self.stopped = job_id

        async def finish_run(*a, **k):
            self.finished = a

        monkeypatch.setattr(create_service.data_plane, "start_run", start_run)
        monkeypatch.setattr(create_service.data_plane, "finish_run", finish_run)
        monkeypatch.setattr(train_spec.data_plane, "prepare_train_data", prepare)
        monkeypatch.setattr(create_service.data_plane, "resolve_stable_package", resolve_stable)
        monkeypatch.setattr(create_service.data_plane, "submit_train", submit)
        monkeypatch.setattr(compute, "stop", stop)


def make_conn(monkeypatch, *, task=TaskType.IMAGE_CLASSIFICATION, inflight=0, existing=None, version=True):
    conn = FakeConn(
        rows={"idempotency_key = $2": existing, "FROM dataset_versions": {"dataset_id": DATASET, "lakefs_commit": "c0ffee"} if version else None,
              "FROM task_presets": preset_row(task) if task is not None else None, "INSERT INTO training_runs": run_row()},
        vals={"task_type": task, "count(*)": inflight},
    )
    patch_tenant_conn(monkeypatch, conn, create_service, service)  # service: fail_unstarted
    return conn


def ctx():
    return SimpleNamespace(tenant_id=str(uuid.uuid4()), actor="user-1")


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
    row = preset_row()
    assert (p.task, p.preset, p.base_model.repo_id, p.base_model.revision) == (TaskType.IMAGE_CLASSIFICATION, "small", row["base_model_repo"], row["base_model_revision"])
    assert (p.learning_rate, p.preprocessing.max_length if hasattr(p.preprocessing, "max_length") else p.preprocessing.rescale) == (row["learning_rate"], row["preprocessing"]["rescale"])
    assert (p.hyperparameters.epochs, p.seed, p.data.lakefs_commit) == (30, 42, "c0ffee")
    assert (p.lr_schedule, p.warmup_fraction, p.augmentation, p.early_stopping_patience) == (row["lr_schedule"], row["warmup_fraction"], row["augmentation"], row["early_stopping_patience"])
    assert (spec.resources.cpu, spec.resources.memory_bytes, spec.resources.gpu, spec.resources.max_runtime_s) == (
        row["cpu"], row["memory_bytes"], row["gpu"], row["max_runtime_s"])
    assert (gw.package_name, gw.package_sha) == (JobPackageName.RAY_TRAINING, PACKAGE_SHA)  # pinned for the run, and what the job runs
    (insert,) = [args for sql, args in conn.calls if sql.startswith("INSERT INTO training_runs")]
    assert (insert[14], insert[16], p.core_sha256, p.bundle_sha256, gw.bundle_name) == (PACKAGE_SHA, None, None, None, None)  # no bundle
    assert gw.ttl >= settings.training.max_runtime_seconds * settings.ray.max_concurrent_training_runs  # outlasts the queue and the run
    insert = next(args for sql, args in conn.calls if sql.startswith("INSERT INTO training_runs"))
    assert insert[4] == spec.job_id and insert[5] == "mlflow-1" and insert[8] == DATASET and insert[10] == VERSION  # job id, MLflow run, dataset, version
    assert insert[13]["payload"]["data"]["lakefs_commit"] == "c0ffee"  # the spec is stored as submitted


def test_hyperparameters_override_only_what_they_name(monkeypatch):
    gw = Gateway(monkeypatch)
    make_conn(monkeypatch)
    create({**BODY, "hyperparameters": {"epochs": 5}, "seed": 7})
    assert (gw.spec.payload.hyperparameters.epochs, gw.spec.payload.hyperparameters.batch_size, gw.spec.payload.seed) == (5, 16, 7)


def _on_a_bundle(monkeypatch):
    from tests.support.bundles import manifest

    conn = make_conn(monkeypatch)
    conn.rows["FROM task_types"] = seeded(TaskType.IMAGE_CLASSIFICATION) | {"bundle_sha256": BUNDLE_SHA, "trainer": "imgcls.train:train"}
    conn.rows["FROM task_presets"] = preset_row(hyperparameters={"epochs": 30, "batch_size": 16, "image_size": 224})  # default values
    conn.rows["FROM job_packages"] = {
        "name": "task_image-classification", "kind": "task", "manifest": manifest(), "sha256": BUNDLE_SHA, "s3_key": "k", "status": "stable",
        "published_by": "ci", "updated_by": "ci", "updated_at": datetime.now(UTC), "smoke_status": "passed", "smoke_job_id": "s"}
    return conn


def test_a_task_on_a_bundle_runs_on_the_core_with_both_shas_pinned(monkeypatch):
    gw = Gateway(monkeypatch)
    conn = _on_a_bundle(monkeypatch)
    create({**BODY, "hyperparameters": {"epochs": 5}})
    p = gw.spec.payload
    assert (p.core_sha256, p.bundle_sha256, gw.package_sha, gw.bundle_name) == (PACKAGE_SHA, BUNDLE_SHA, PACKAGE_SHA, "task_image-classification")
    assert (p.trainer, p.hyperparameters.epochs, p.hyperparameters.batch_size) == ("imgcls.train:train", 5, 16)  # the preset's defaults fill in
    (insert,) = [args for sql, args in conn.calls if sql.startswith("INSERT INTO training_runs")]
    assert (insert[14], insert[16], insert[7]["epochs"]) == (PACKAGE_SHA, BUNDLE_SHA, 5)


def test_a_bundle_schema_refuses_a_bad_hyperparameter_with_422_and_nothing_starts(monkeypatch):
    gw = Gateway(monkeypatch)
    _on_a_bundle(monkeypatch)
    for asked in ({"epochs": 501}, {"image_size": 8}):  # outside the bundle schema's bounds
        with pytest.raises(Problem) as err:
            create({**BODY, "hyperparameters": asked})
        assert (err.value.status_code, err.value.code) == (422, ProblemCode.INVALID_HYPERPARAMETERS) and not gw.started


def test_a_bundle_key_beyond_the_typed_three_reaches_the_spec(monkeypatch):
    gw = Gateway(monkeypatch)
    _on_a_bundle(monkeypatch)
    create({**BODY, "hyperparameters": {"momentum": 0.9}})  # the bundle's schema leaves its properties open
    assert gw.spec.payload.hyperparameters["momentum"] == 0.9


def test_a_disabled_preset_is_unknown(monkeypatch):
    gw = Gateway(monkeypatch)
    conn = make_conn(monkeypatch)
    conn.rows["FROM task_presets"] = preset_row(enabled=False)
    with pytest.raises(Problem) as err:
        create()
    assert err.value.code == ProblemCode.UNKNOWN_PRESET and not gw.started


def test_a_preset_the_task_does_not_have_is_422_and_nothing_starts(monkeypatch):
    gw = Gateway(monkeypatch)
    conn = make_conn(monkeypatch)
    conn.rows["FROM task_presets"] = None
    with pytest.raises(Problem) as err:
        create({**BODY, "preset": "xlarge"})
    assert (err.value.status_code, err.value.code) == (422, ProblemCode.UNKNOWN_PRESET) and not gw.started


@pytest.mark.parametrize("name,asked", [("epochs", 501), ("batch_size", 513), ("image_size", 31)])
def test_a_hyperparameter_outside_the_presets_bounds_is_422_and_nothing_starts(monkeypatch, name, asked):
    gw = Gateway(monkeypatch)
    make_conn(monkeypatch)
    with pytest.raises(Problem) as err:
        create({**BODY, "hyperparameters": {name: asked}})
    assert (err.value.status_code, err.value.code) == (422, ProblemCode.HYPERPARAMETER_OUT_OF_RANGE) and not gw.started


def test_a_silent_request_gets_the_presets_defaults(monkeypatch):
    gw = Gateway(monkeypatch)
    conn = make_conn(monkeypatch)
    conn.rows["FROM task_presets"] = preset_row(hyperparameters={**LIMITS, "epochs": {"default": 12, "min": 1, "max": 20}})
    create()
    assert gw.spec.payload.hyperparameters.epochs == 12


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
    make_conn(monkeypatch, existing=run_row(preset="large"))
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
            if "FROM task_presets" in sql:
                return preset_row()
            if "FROM task_types" in sql:
                return seeded(args[0])
            if "FROM plan_limits" in sql:
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
    assert failed[2] == TrainingStatus.FAILED and failed[4]["code"] == FailureCode.INTERNAL


# ---- lifecycle: the sweep drives decide -> effects -> persist, against a table that enforces the compare-and-set ----

def test_a_finished_run_cannot_move_again():
    assert TrainingStatus.RUNNING not in TRANSITIONS[TrainingStatus.SUCCEEDED]
    assert TrainingStatus.SUBMITTED not in TRANSITIONS[TrainingStatus.RUNNING]
    assert all(not TRANSITIONS[s] for s in (TrainingStatus.SUCCEEDED, TrainingStatus.FAILED, TrainingStatus.CANCELLED))


class Fx:
    """Everything a settling run touches outside the table, recorded; `table.open` is sampled at each call so a test
    can assert that no connection was held across it."""

    def __init__(self, monkeypatch, report=None):
        self.table = FakeJobTable(persist.TRAINING)
        self.report, self.ray = report, JobStatus.RUNNING
        self.mlflow, self.published, self.registered, self.usage, self.metrics, self.stopped = [], [], [], [], [], []
        self.open_during = []
        patch(monkeypatch, self.table, service)

        async def status(job_id):
            if isinstance(self.ray, Exception):
                raise self.ray
            return self.ray

        async def read_train_report(*ids):
            self.open_during.append(self.table.open)
            if isinstance(self.report, Exception):
                raise self.report
            return self.report

        async def finish_run(tenant_id, project_id, run_id, status, metrics=None):
            self.open_during.append(self.table.open)
            self.mlflow.append((status, metrics))

        async def publish_run_model(*ids):
            self.open_during.append(self.table.open)
            self.published.append(ids)

        async def register_in_mlflow(tenant_id, project_id, name, mlflow_run_id):
            self.open_during.append(self.table.open)
            return RegisteredVersion(mlflow_model_name=f"{name}-{mlflow_run_id}", mlflow_version="1")

        async def record_version(conn, tenant_id, project_id, run_id, registered):
            self.registered.append((run_id, registered.mlflow_model_name, registered.mlflow_version))

        async def record_usage(conn, **kw):
            self.usage.append(kw)

        async def stop(job_id):
            self.stopped.append(job_id)

        monkeypatch.setattr(compute, "status", status)
        monkeypatch.setattr(compute, "stop", stop)
        self.ray_start = None  # what Ray's own record says about when the job started

        async def job_state(job_id):
            return compute.JobState(status=self.ray, started_at=self.ray_start)

        monkeypatch.setattr(compute, "job_state", job_state)
        self.purged = []

        async def purge_train_prefix(tenant_id, project_id, run_id):
            self.purged.append(run_id)

        monkeypatch.setattr(effects.data_plane, "purge_train_prefix", purge_train_prefix)
        monkeypatch.setattr(effects.data_plane, "read_train_report", read_train_report)
        monkeypatch.setattr(effects.data_plane, "finish_run", finish_run)
        monkeypatch.setattr(effects.data_plane, "publish_run_model", publish_run_model)
        monkeypatch.setattr(effects, "register_in_mlflow", register_in_mlflow)
        monkeypatch.setattr(persist, "record_version", record_version)
        monkeypatch.setattr(persist, "record_usage", record_usage)
        monkeypatch.setattr(service, "usage_metrics", SimpleNamespace(training_runs=SimpleNamespace(add=lambda n, attrs: self.metrics.append(attrs["status"]))))

    def seed(self, status="running", **kw):
        base = {"project_id": PROJECT, "mlflow_run_id": "mlflow-1", "ray_job_id": f"train-{RUN}", "created_at": datetime.now(UTC),
                "started_at": datetime.now(UTC) - timedelta(seconds=60), "spec": {"resources": {"cpu": 1, "memory_bytes": 1, "gpu": 1}},
                "metrics": None, "report": None}
        return self.table.seed(status, **{**base, **kw})

    def sweep(self):
        asyncio.run(service.reconcile_inflight_runs())


GOOD = TrainReport(status=TrainOutcome.SUCCEEDED, metrics={"top1": 0.9}, history=[{"epoch": 1, "loss": 0.5}])


def test_a_succeeded_job_with_a_good_report_logs_metrics_registers_the_model_and_bills(monkeypatch):
    fx = Fx(monkeypatch, GOOD)
    row = fx.seed()
    fx.ray = JobStatus.SUCCEEDED
    fx.sweep()
    assert row["status"] == TrainingStatus.SUCCEEDED and row["completed_at"] is not None and row["claimed_until"] is None
    assert row["metrics"] == {"top1": 0.9} and row["report"]["history"] == [{"epoch": 1, "loss": 0.5}]  # the whole report is kept
    assert fx.mlflow == [("FINISHED", {"top1": 0.9})] and len(fx.published) == 1
    assert fx.registered == [(row["id"], f"{DEFAULT_MODEL_NAME}-mlflow-1", "1")]
    assert fx.usage[0]["source_ref"] == f"train-{RUN}" and fx.usage[0]["unit"] == "gpu_seconds"
    assert fx.metrics == [TrainingStatus.SUCCEEDED] and fx.purged == [str(row["id"])]  # the staged files go once the run is final
    assert fx.open_during == [0, 0, 0, 0]  # report read, MLflow closed, model published and registered with no connection held


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
def test_a_job_that_ends_ok_but_reports_failure_or_nothing_is_failed_and_billed_for_its_runtime(monkeypatch, report):
    fx = Fx(monkeypatch, report)
    row = fx.seed()
    fx.ray = JobStatus.SUCCEEDED  # Ray said SUCCEEDED
    fx.sweep()
    assert row["status"] == TrainingStatus.FAILED and row["report"]["status"] == "failed"
    assert row["report"]["code"] == (report.code if isinstance(report, TrainReport) else FailureCode.INTERNAL)
    assert fx.mlflow[0][0] == "FAILED" and not fx.published and not fx.registered and len(fx.usage) == 1
    assert fx.metrics == [TrainingStatus.FAILED]


def test_a_report_that_says_succeeded_does_not_save_a_job_ray_failed(monkeypatch):
    fx = Fx(monkeypatch, GOOD)
    row = fx.seed()
    fx.ray = JobStatus.FAILED
    fx.sweep()
    assert row["status"] == TrainingStatus.FAILED and not fx.published and not fx.registered and len(fx.usage) == 1


def test_a_stopped_job_cancels_the_run_and_kills_the_mlflow_run(monkeypatch):
    fx = Fx(monkeypatch)
    row = fx.seed()
    fx.ray = JobStatus.STOPPED
    fx.sweep()
    assert row["status"] == TrainingStatus.CANCELLED and row["completed_at"] is not None and row["report"] is None
    assert fx.mlflow == [("KILLED", None)] and fx.metrics == [TrainingStatus.CANCELLED]
    assert fx.purged == [str(row["id"])]


def test_a_purge_failure_does_not_fail_the_settle(monkeypatch):
    fx = Fx(monkeypatch, GOOD)
    row = fx.seed()
    fx.ray = JobStatus.SUCCEEDED

    async def boom(*ids):
        raise RuntimeError("s3 down")

    monkeypatch.setattr(effects.data_plane, "purge_train_prefix", boom)
    fx.sweep()
    assert row["status"] == TrainingStatus.SUCCEEDED


def test_a_run_ray_starts_moves_to_running_and_one_still_pending_stays_submitted(monkeypatch):
    fx = Fx(monkeypatch)
    pending, running = fx.seed("submitted"), fx.seed("submitted")
    fx.ray = JobStatus.PENDING
    fx.sweep()
    assert pending["status"] == TrainingStatus.SUBMITTED
    fx.ray = JobStatus.RUNNING
    fx.sweep()
    assert running["status"] == TrainingStatus.RUNNING and not fx.mlflow and not fx.metrics
    fx.sweep()  # running -> running is not an edge: nothing to do
    assert running["status"] == TrainingStatus.RUNNING and running["completed_at"] is None


def test_the_sweep_ignores_a_run_someone_else_just_moved(monkeypatch):
    fx = Fx(monkeypatch, GOOD)
    row = fx.seed()
    fx.ray = JobStatus.SUCCEEDED
    original = effects.data_plane.read_train_report

    async def read_then_cancel(*ids):
        row["status"] = TrainingStatus.CANCELLED  # a cancel lands between the poll and the write
        return await original(*ids)

    monkeypatch.setattr(effects.data_plane, "read_train_report", read_then_cancel)
    fx.sweep()
    assert row["status"] == TrainingStatus.CANCELLED
    assert not fx.registered and not fx.usage and not fx.metrics  # only the writer that wins bills and registers


def test_two_replicas_sweeping_at_once_bill_a_finished_run_once(monkeypatch):
    fx = Fx(monkeypatch, GOOD)
    rows = [fx.seed() for _ in range(5)]
    fx.ray = JobStatus.SUCCEEDED

    async def both():
        await asyncio.gather(service.reconcile_inflight_runs(), service.reconcile_inflight_runs())

    asyncio.run(both())
    assert {r["status"] for r in rows} == {TrainingStatus.SUCCEEDED}
    assert len(fx.usage) == len(fx.registered) == len(fx.metrics) == 5


def test_a_gateway_failure_leaves_the_run_in_flight_and_the_next_sweep_settles_it(monkeypatch):
    fx = Fx(monkeypatch, GOOD)
    row = fx.seed()
    fx.ray = JobStatus.SUCCEEDED
    working = effects.data_plane.publish_run_model

    async def broken(*ids):
        raise RuntimeError("mlflow down")

    monkeypatch.setattr(effects.data_plane, "publish_run_model", broken)
    fx.sweep()
    backoff = timedelta(seconds=job_lifecycle.SETTLE_BACKOFF_BASE_SECONDS)
    assert row["status"] == TrainingStatus.RUNNING and not fx.usage
    assert row["settle_attempts"] == 1 and row["claimed_until"] == fx.table.now + backoff  # retried after its backoff, not after the lease
    monkeypatch.setattr(effects.data_plane, "publish_run_model", working)
    fx.table.now += backoff + timedelta(seconds=1)
    fx.sweep()
    assert row["status"] == TrainingStatus.SUCCEEDED and len(fx.usage) == 1


def test_a_run_ray_no_longer_knows_fails_only_past_the_deadline_and_the_job_is_stopped(monkeypatch):
    fx = Fx(monkeypatch)
    fresh = fx.seed("submitted")
    old = fx.seed("submitted", created_at=datetime.now(UTC) - timedelta(seconds=train_spec.horizon_seconds() + 60))
    fx.ray = RuntimeError("404 from the dashboard")
    fx.sweep()
    assert (fresh["status"], old["status"]) == (TrainingStatus.SUBMITTED, TrainingStatus.FAILED)
    assert old["report"]["code"] == FailureCode.TIMEOUT and fx.stopped == [old["ray_job_id"]]
    assert fx.mlflow == [("FAILED", {})] and fx.metrics == [TrainingStatus.FAILED]


def test_a_job_still_running_past_the_horizon_is_timed_out_and_stopped(monkeypatch):
    fx = Fx(monkeypatch)
    row = fx.seed(created_at=datetime.now(UTC) - timedelta(seconds=train_spec.horizon_seconds() + 60))
    fx.sweep()  # Ray still answers RUNNING
    assert row["status"] == TrainingStatus.FAILED and row["report"]["code"] == FailureCode.TIMEOUT and fx.stopped == [row["ray_job_id"]]


def test_a_job_that_finished_wins_over_the_horizon(monkeypatch):
    fx = Fx(monkeypatch, GOOD)
    row = fx.seed(created_at=datetime.now(UTC) - timedelta(seconds=train_spec.horizon_seconds() + 60))
    fx.ray = JobStatus.SUCCEEDED
    fx.sweep()
    assert row["status"] == TrainingStatus.SUCCEEDED and not fx.stopped


def test_a_fast_job_never_seen_running_is_billed_from_rays_start_time(monkeypatch):
    fx = Fx(monkeypatch, GOOD)
    row = fx.seed("submitted", started_at=None, created_at=datetime.now(UTC) - timedelta(hours=1))  # queued for an hour
    fx.ray, fx.ray_start = JobStatus.SUCCEEDED, datetime.now(UTC) - timedelta(seconds=40)
    fx.sweep()
    assert row["status"] == TrainingStatus.SUCCEEDED and row["started_at"] == fx.ray_start
    assert fx.usage[0]["quantity"] == pytest.approx(40, abs=2)  # 1 GPU, the 40 s Ray ran, not the hour in the queue


def test_a_fast_job_ray_cannot_date_is_not_billed_and_not_from_created_at(monkeypatch):
    fx = Fx(monkeypatch, GOOD)
    row = fx.seed("submitted", started_at=None, created_at=datetime.now(UTC) - timedelta(hours=1))
    fx.ray = JobStatus.SUCCEEDED  # ray_start stays None
    fx.sweep()
    assert row["status"] == TrainingStatus.SUCCEEDED and row["started_at"] is None and not fx.usage


def test_rays_start_time_does_not_override_a_stamped_started_at(monkeypatch):
    fx = Fx(monkeypatch, GOOD)
    row = fx.seed()
    stamped = row["started_at"]
    fx.ray, fx.ray_start = JobStatus.SUCCEEDED, datetime.now(UTC) - timedelta(hours=1)
    fx.sweep()
    assert row["status"] == TrainingStatus.SUCCEEDED and row["started_at"] == stamped


def test_a_stopped_job_never_seen_running_is_not_dated_by_ray(monkeypatch):
    fx = Fx(monkeypatch)
    row = fx.seed("submitted", started_at=None)
    fx.ray, fx.ray_start = JobStatus.STOPPED, datetime.now(UTC) - timedelta(seconds=40)
    fx.sweep()
    assert row["status"] == TrainingStatus.CANCELLED and not fx.usage


# ---- the enums and the database agree ----

def test_the_status_check_mirrors_the_enum():
    sql = (MIGRATIONS / "20261017172706_init.sql").read_text()
    clause = re.search(r"status\s+text NOT NULL DEFAULT 'submitted'\s+CHECK \(status IN \(([^)]*)\)\)", sql)
    assert set(re.findall(r"'([^']+)'", clause.group(1))) == {s.value for s in TrainingStatus}


# ---- reads and failure paths the tests above do not reach ----

def test_get_run_returns_the_run(call):
    ok = call("GET", f"{BASE}/{RUN}", {**access(), **run_row()})
    assert (ok.status_code, ok.json()["id"]) == (200, str(RUN))


def test_get_run_of_another_project_is_404(monkeypatch):
    patch_tenant_conn(monkeypatch, FakeConn(row=None), run_service)
    with pytest.raises(Problem) as err:
        asyncio.run(run_service.get_run(ctx(), PROJECT, RUN))
    assert err.value.status_code == 404


def test_logs_of_an_unknown_run_are_404(monkeypatch):
    patch_tenant_conn(monkeypatch, FakeConn(row=None), run_service)
    with pytest.raises(Problem) as err:
        asyncio.run(run_service.get_logs(ctx(), PROJECT, RUN))
    assert err.value.status_code == 404


def test_cancel_route_stops_a_viewer_but_lets_a_deployer_cancel(call, monkeypatch):
    async def cancel_run(c, project_id, run_id):
        return run_row(status="cancelled")

    monkeypatch.setattr(run_service, "cancel_run", cancel_run)
    r = call("POST", f"{BASE}/{RUN}/cancel", access(role=TenantRole.ADMIN))
    assert (r.status_code, r.json()["status"]) == (200, "cancelled")


def test_presets_route_lists_the_projects_task_presets(monkeypatch, dev_headers):
    from app.modules.user.training import preset_service

    client = make_client(routes.presets_router)
    conn = FakeConn(access(), val=TaskType.IMAGE_CLASSIFICATION, rowset=[preset_row()])
    patch_tenant_conn(monkeypatch, conn, preset_service)
    r = client.get(f"/v1/projects/{PROJECT}/training-presets")
    assert r.status_code == 200 and [p["name"] for p in r.json()] == ["small"]
    assert conn.calls[-1][1] == (TaskType.IMAGE_CLASSIFICATION,)  # asked for this task's presets only


def test_a_project_without_a_task_type_has_no_presets(monkeypatch, dev_headers):
    from app.modules.user.training import preset_service

    client = make_client(routes.presets_router)
    patch_tenant_conn(monkeypatch, FakeConn(access(), val=None), preset_service)
    r = client.get(f"/v1/projects/{PROJECT}/training-presets")
    assert (r.status_code, r.json()) == (200, [])


@pytest.mark.parametrize("broken", ["resolve", "start_run"])
def test_a_failure_before_the_run_exists_is_a_503_and_nothing_is_stored_or_submitted(monkeypatch, broken):
    gw = Gateway(monkeypatch)
    conn = make_conn(monkeypatch)

    async def boom(*a, **k):
        raise RuntimeError("mlflow is down")

    monkeypatch.setattr(create_service.data_plane, "resolve_stable_package" if broken == "resolve" else "start_run", boom)
    with pytest.raises(Problem) as err:
        create()
    assert (err.value.status_code, err.value.code) == (503, ProblemCode.START_FAILED)
    assert gw.spec is None and not any(sql.startswith("INSERT INTO training_runs") for sql, _ in conn.calls)


def test_a_submit_failure_still_fails_the_run_when_stopping_the_job_and_closing_the_run_also_fail(monkeypatch):
    gw = Gateway(monkeypatch, fail_submit=True)
    make_conn(monkeypatch)

    async def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(create_service.job_lifecycle.compute, "stop", boom)
    monkeypatch.setattr(service, "fail_unstarted", boom)
    with pytest.raises(Problem) as err:
        create()
    assert (err.value.status_code, err.value.code) == (503, ProblemCode.START_FAILED) and gw.spec is not None
