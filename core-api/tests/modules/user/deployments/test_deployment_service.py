"""Deployment service rules, called directly (no HTTP)."""
import uuid

import asyncio

import pytest
from fastapi import HTTPException

from types import SimpleNamespace
from app.core.enums import Split, TaskType
from app.modules.user.deployments import service
from app.modules.user.deployments.constants import (
    METRIC_FLOORS,
    PLAN_TIER_RATE_LIMITS,
    SETTING_DEPLOY_STRATEGY,
    DeployStrategy,
    PlanTier,
    ProblemCode,
)
from app.integrations.serve_client import Prediction
from app.modules.user.deployments.predict import Answered
from app.modules.user.deployments.status import DEPLOYMENTS, DeploymentStatus
from app.modules.user.training import TrainingStatus
from tests.support import FakeConn
from tests.support.counted_conn import patch_counted_conn

CTX = SimpleNamespace(tenant_id=str(uuid.uuid4()), actor="user-1")


def test_rate_limit_follows_plan_tier_and_unknown_falls_back_to_free():
    assert service.rate_limit_for(PlanTier.PRO) == PLAN_TIER_RATE_LIMITS[PlanTier.PRO]
    assert service.rate_limit_for("pro") == PLAN_TIER_RATE_LIMITS[PlanTier.PRO]
    assert service.rate_limit_for(None) == PLAN_TIER_RATE_LIMITS[PlanTier.FREE]


PROJECT, RUN = uuid.uuid4(), uuid.uuid4()
KEY = "k"
SERVABLE = {"id": RUN, "status": TrainingStatus.SUCCEEDED, "mlflow_run_id": "mlflow-1", "class_names": ["cat", "dog"], "test_dataset_id": uuid.uuid4()}


@pytest.fixture
def deploy_env(monkeypatch):
    """create_deployment with a fake database."""
    class Env:
        conn = FakeConn(
            rows={"idempotency_key = $2": None, "FROM training_runs": SERVABLE, "INSERT INTO deployments": None},
            vals={"count(*) FROM deployments": 0, "SELECT settings": {}, "SELECT plan_tier": "pro", "SELECT task_type": TaskType.IMAGE_CLASSIFICATION},
        )

        @property
        def deployed(self):
            return [args for sql, args in self.conn.calls if sql.startswith(("INSERT INTO deployments", "UPDATE deployments"))]

    e = Env()
    e.opened = patch_counted_conn(monkeypatch, e.conn, service)
    return e


def inserted(conn):
    return next(args for sql, args in conn.calls if sql.startswith("INSERT INTO deployments"))


def test_deploy_mints_a_deployment_and_a_predict_key_shown_once(deploy_env):
    deploy_env.conn.rows["INSERT INTO deployments"] = {
        "id": uuid.uuid4(), "status": "active", "route_prefix": "/predict/x", "rate_limit_per_minute": PLAN_TIER_RATE_LIMITS[PlanTier.PRO],
    }
    out = asyncio.run(service.create_deployment(CTX, PROJECT, RUN, KEY))
    assert (out.status, out.plan_tier, out.rate_limit_per_minute) == ("active", "pro", PLAN_TIER_RATE_LIMITS[PlanTier.PRO])
    args = inserted(deploy_env.conn)
    assert args[1:6] == (CTX.tenant_id, PROJECT, RUN, None, f"/predict/{args[0]}")
    assert out.predict_key and args[6] == service.hash_key(out.predict_key)  # only the hash is stored


def test_a_main_strategy_redeploy_updates_the_one_deployment_in_place(deploy_env):
    main_id = uuid.uuid4()
    deploy_env.conn.vals["SELECT settings"] = {SETTING_DEPLOY_STRATEGY: DeployStrategy.MAIN}
    deploy_env.conn.rows["FROM deployments"] = {"id": main_id, "status": DeploymentStatus.FAILED}
    deploy_env.conn.rows["UPDATE deployments"] = {
        "id": main_id, "tenant_id": CTX.tenant_id, "project_id": PROJECT, "status": "active", "route_prefix": "/predict/p/main", "rate_limit_per_minute": 10}
    out = asyncio.run(service.create_deployment(CTX, PROJECT, RUN, KEY))
    assert deploy_env.deployed[0][:4] == (main_id, DeploymentStatus.FAILED, DeploymentStatus.ACTIVE, RUN)  # compare-and-set
    assert out.id == main_id and out.predict_key is None  # same URL, same key: nothing new is minted
    assert not any(sql.startswith("INSERT INTO deployments") for sql, _ in deploy_env.conn.calls)


def test_a_main_strategy_redeploy_never_resurrects_a_stopped_deployment(deploy_env):
    deploy_env.conn.vals["SELECT settings"] = {SETTING_DEPLOY_STRATEGY: DeployStrategy.MAIN}
    deploy_env.conn.rows["FROM deployments"] = {"id": uuid.uuid4(), "status": "stopped", "predict_key_hash": "h", "rate_limit_per_minute": 10}
    with pytest.raises(HTTPException) as e:
        asyncio.run(service.create_deployment(CTX, PROJECT, RUN, KEY))
    assert (e.value.status_code, e.value.code) == (409, ProblemCode.STOPPED) and not deploy_env.deployed


def test_a_main_strategy_redeploy_that_loses_the_race_to_a_stop_is_409(deploy_env):
    deploy_env.conn.vals["SELECT settings"] = {SETTING_DEPLOY_STRATEGY: DeployStrategy.MAIN}
    deploy_env.conn.rows["FROM deployments"] = {"id": uuid.uuid4(), "status": "active", "predict_key_hash": "h", "rate_limit_per_minute": 10}
    deploy_env.conn.rows["UPDATE deployments"] = None  # the UPDATE only touches a row that is not stopped
    with pytest.raises(HTTPException) as e:
        asyncio.run(service.create_deployment(CTX, PROJECT, RUN, KEY))
    assert e.value.code == ProblemCode.STOPPED


def test_the_main_lookup_leaves_deleted_rows_out_so_a_new_key_is_minted(deploy_env):
    deploy_env.conn.vals["SELECT settings"] = {SETTING_DEPLOY_STRATEGY: DeployStrategy.MAIN}
    deploy_env.conn.rows["FROM deployments"] = None
    deploy_env.conn.rows["INSERT INTO deployments"] = {"id": uuid.uuid4(), "status": "deploying", "route_prefix": "/p", "rate_limit_per_minute": 10}
    assert asyncio.run(service.create_deployment(CTX, PROJECT, RUN, KEY)).predict_key
    sql, args = next((s, a) for s, a in deploy_env.conn.calls if "route_prefix = $2" in s)
    assert "status <> $3" in sql and args[2] == "deleted"


@pytest.mark.parametrize("row", [
    {**SERVABLE, "status": TrainingStatus.FAILED},
    {**SERVABLE, "status": TrainingStatus.RUNNING},
    {**SERVABLE, "mlflow_run_id": None},
    {**SERVABLE, "class_names": []},  # not a classification model: the serving script cannot serve it
])
def test_deploy_requires_a_succeeded_servable_run(deploy_env, row):
    deploy_env.conn.rows["FROM training_runs"] = row
    with pytest.raises(HTTPException) as e:
        asyncio.run(service.create_deployment(CTX, PROJECT, RUN, KEY))
    assert e.value.status_code == 409 and not deploy_env.deployed


def test_a_run_without_a_test_dataset_needs_acknowledge_risk_and_the_answer_is_recorded(deploy_env):
    deploy_env.conn.rows["FROM training_runs"] = {**SERVABLE, "test_dataset_id": None}
    with pytest.raises(HTTPException) as e:
        asyncio.run(service.create_deployment(CTX, PROJECT, RUN, KEY))
    assert e.value.status_code == 409 and e.value.code == ProblemCode.DEPLOY_RISK and not deploy_env.deployed
    deploy_env.conn.rows["INSERT INTO deployments"] = {"id": uuid.uuid4(), "status": "deploying", "route_prefix": "/predict/x", "rate_limit_per_minute": 60}
    asyncio.run(service.create_deployment(CTX, PROJECT, RUN, KEY, acknowledge_risk=True))
    assert deploy_env.deployed and inserted(deploy_env.conn)[-1] is True  # risk_acknowledged is the last insert argument


def stats(splits, row_count):
    return {"file_count": 0, "total_bytes": 0, "row_count": row_count, "splits": splits}


def test_the_gate_reads_the_test_split_from_the_runs_dataset_version_not_only_a_test_dataset(deploy_env):
    deploy_env.conn.rows["INSERT INTO deployments"] = {"id": uuid.uuid4(), "status": "deploying", "route_prefix": "/predict/x", "rate_limit_per_minute": 60}
    no_dataset = {**SERVABLE, "test_dataset_id": None}
    for version_stats, risky in (
        (stats({Split.TRAIN: 8, Split.VALIDATION: 2}, 10), True),  # every row split, none of them test
        (stats({Split.TRAIN: 8, Split.TEST: 2}, 10), False),  # test rows the user supplied
        (stats({}, 10), False),  # rows with no split are hashed, and some of them land in test
        (None, True),  # a run from before dataset versions
    ):
        deploy_env.conn.rows["FROM training_runs"] = {**no_dataset, "version_stats": version_stats}
        if risky:
            with pytest.raises(HTTPException) as e:
                asyncio.run(service.create_deployment(CTX, PROJECT, RUN, KEY))
            assert e.value.code == ProblemCode.DEPLOY_RISK
        else:
            asyncio.run(service.create_deployment(CTX, PROJECT, RUN, KEY))
            assert inserted(deploy_env.conn)[-1] is False  # no acknowledgement was needed


def test_a_score_under_the_task_floor_needs_acknowledge_risk(deploy_env):
    metric, floor = METRIC_FLOORS[TaskType.IMAGE_CLASSIFICATION]  # the fixture's default task
    deploy_env.conn.rows["FROM training_runs"] = {**SERVABLE, "metrics": {metric: floor - 0.1}}
    with pytest.raises(HTTPException) as e:
        asyncio.run(service.create_deployment(CTX, PROJECT, RUN, KEY))
    assert e.value.status_code == 409 and e.value.code == ProblemCode.DEPLOY_RISK and metric in e.value.detail and not deploy_env.deployed
    deploy_env.conn.rows["FROM training_runs"] = {**SERVABLE, "metrics": {metric: floor}}  # at the floor is fine
    deploy_env.conn.rows["INSERT INTO deployments"] = {"id": uuid.uuid4(), "status": "deploying", "route_prefix": "/predict/x", "rate_limit_per_minute": 60}
    asyncio.run(service.create_deployment(CTX, PROJECT, RUN, KEY))
    assert deploy_env.deployed and inserted(deploy_env.conn)[-1] is False


def test_a_detection_run_deploys_with_the_class_names_its_report_lists(deploy_env):
    deploy_env.conn.vals["SELECT task_type"] = TaskType.OBJECT_DETECTION
    deploy_env.conn.rows["FROM training_runs"] = {**SERVABLE, "class_names": ["person", "car"]}
    deploy_env.conn.rows["INSERT INTO deployments"] = {
        "id": uuid.uuid4(), "status": "deploying", "route_prefix": "/predict/x", "rate_limit_per_minute": PLAN_TIER_RATE_LIMITS[PlanTier.PRO],
    }
    assert asyncio.run(service.create_deployment(CTX, PROJECT, RUN, KEY)).predict_key
    assert inserted(deploy_env.conn)[3] == RUN  # the predictor apps read the class names from the run on the first request


@pytest.mark.parametrize("task", [None, TaskType.TABULAR_CLASSIFICATION, TaskType.TEXT_GENERATION])
def test_deploy_requires_a_servable_task_whatever_the_report_carries(deploy_env, task):
    deploy_env.conn.vals["SELECT task_type"] = task  # a tabular classifier's report has labels too
    with pytest.raises(HTTPException) as e:
        asyncio.run(service.create_deployment(CTX, PROJECT, RUN, KEY))
    assert e.value.status_code == 409 and not deploy_env.deployed


def test_deploy_of_another_projects_run_is_404(deploy_env):
    deploy_env.conn.rows["FROM training_runs"] = None
    with pytest.raises(HTTPException) as e:
        asyncio.run(service.create_deployment(CTX, PROJECT, RUN, KEY))
    assert e.value.status_code == 404 and not deploy_env.deployed


def test_test_deployment_releases_the_connection_before_calling_serve(monkeypatch):
    open_conns = 0

    class Ctx:
        async def __aenter__(self):
            nonlocal open_conns
            open_conns += 1
            return FakeConn(row={"status": "active", "training_run_id": uuid.uuid4()})

        async def __aexit__(self, *a):
            nonlocal open_conns
            open_conns -= 1

    monkeypatch.setattr(service, "TenantConn", lambda tenant_id: Ctx())

    async def answer(*_):
        assert open_conns == 0
        return Answered(run_id=uuid.uuid4(), request_bytes=2, prediction=Prediction(predictions={"ok": 1}, cold_load=False), elapsed_ms=1.5)

    monkeypatch.setattr(service.predict, "answer", answer)
    out = asyncio.run(service.test_deployment(CTX, uuid.uuid4(), uuid.uuid4(), {"input": {}}))
    assert (out.response, out.latency_ms) == ({"predictions": {"ok": 1}}, 1.5)


def test_every_status_is_declared_and_deleted_is_final():
    edges = DEPLOYMENTS.transitions
    assert set(edges) | {t for ts in edges.values() for t in ts} == set(DeploymentStatus)
    assert DeploymentStatus.DELETED not in edges and all(DeploymentStatus.DELETED in ts for ts in edges.values())
