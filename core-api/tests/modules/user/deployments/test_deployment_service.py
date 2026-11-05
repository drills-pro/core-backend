"""Deployment service rules, called directly (no HTTP)."""
import uuid

import asyncio

import pytest
from fastapi import HTTPException

from types import SimpleNamespace
from app.core.enums import TaskType
from app.modules.user.deployments import service
from app.modules.user.deployments.constants import (
    DEFAULT_COMPUTE_TARGET,
    PLAN_TIER_RATE_LIMITS,
    SETTING_COMPUTE_TARGET,
    SETTING_DEPLOY_STRATEGY,
    DeployStrategy,
    PlanTier,
    ProblemCode,
)
from app.integrations.serve_client import ComputeTarget
from app.modules.user.training import TrainingStatus
from tests.support import FakeConn
from tests.support.counted_conn import patch_counted_conn

CTX = SimpleNamespace(tenant_id=str(uuid.uuid4()))


def test_rate_limit_follows_plan_tier_and_unknown_falls_back_to_free():
    assert service.rate_limit_for(PlanTier.PRO) == PLAN_TIER_RATE_LIMITS[PlanTier.PRO]
    assert service.rate_limit_for("pro") == PLAN_TIER_RATE_LIMITS[PlanTier.PRO]
    assert service.rate_limit_for(None) == PLAN_TIER_RATE_LIMITS[PlanTier.FREE]


PROJECT, RUN = uuid.uuid4(), uuid.uuid4()
SERVABLE = {"id": RUN, "status": TrainingStatus.SUCCEEDED, "mlflow_run_id": "mlflow-1", "class_names": ["cat", "dog"]}


@pytest.fixture
def deploy_env(monkeypatch):
    """create_deployment with a fake database and a fake Serve deploy that fails if a connection is held."""
    class Env:
        conn = FakeConn(
            rows={"FROM training_runs": SERVABLE, "INSERT INTO deployments": None},
            vals={"SELECT settings": {}, "SELECT plan_tier": "pro", "SELECT task_type": TaskType.IMAGE_CLASSIFICATION},
        )
        deployed = []

    e = Env()
    e.opened = patch_counted_conn(monkeypatch, e.conn, service)

    async def deploy(**kw):
        assert e.opened.now == 0, "Serve deploy called while a connection was held"
        e.deployed.append(kw)
        return "ray-job-1", f"/predict/{kw['deployment_id']}"

    async def no_shared_apps():
        return False  # the legacy path; the shared one is in test_shared_deployments.py

    monkeypatch.setattr(service.serve_client, "deploy", deploy)
    monkeypatch.setattr(service.serve_client, "apps_declared", no_shared_apps)
    return e


def inserted(conn):
    return next(args for sql, args in conn.calls if sql.startswith("INSERT INTO deployments"))


def test_deploy_mints_a_deployment_and_a_predict_key_shown_once(deploy_env):
    deploy_env.conn.rows["INSERT INTO deployments"] = {
        "id": uuid.uuid4(), "status": "deploying", "route_prefix": "/predict/x", "rate_limit_per_minute": PLAN_TIER_RATE_LIMITS[PlanTier.PRO],
    }
    out = asyncio.run(service.create_deployment(CTX, PROJECT, RUN))
    kw = deploy_env.deployed[0]
    assert (kw["mlflow_run_id"], kw["class_names"], kw["compute_target"], kw["deploy_strategy"]) == (
        "mlflow-1", ["cat", "dog"], DEFAULT_COMPUTE_TARGET, DeployStrategy.LATEST)
    assert (out.status, out.plan_tier, out.rate_limit_per_minute) == ("deploying", "pro", PLAN_TIER_RATE_LIMITS[PlanTier.PRO])
    args = inserted(deploy_env.conn)
    assert args[1:5] == (CTX.tenant_id, PROJECT, RUN, "ray-job-1")
    assert out.predict_key and args[6] == service.hash_key(out.predict_key)  # only the hash is stored
    assert deploy_env.opened.total == 2  # read, then write: Serve is deployed in between


def test_a_main_strategy_redeploy_updates_the_one_deployment_in_place(deploy_env):
    main_id = uuid.uuid4()
    deploy_env.conn.vals["SELECT settings"] = {SETTING_DEPLOY_STRATEGY: DeployStrategy.MAIN, SETTING_COMPUTE_TARGET: ComputeTarget.GPU}
    deploy_env.conn.rows["FROM deployments"] = {"id": main_id, "status": "active", "predict_key_hash": "h", "rate_limit_per_minute": 10}
    deploy_env.conn.rows["UPDATE deployments"] = {"id": main_id, "status": "deploying", "route_prefix": "/predict/p/main", "rate_limit_per_minute": 10}
    out = asyncio.run(service.create_deployment(CTX, PROJECT, RUN))
    kw = deploy_env.deployed[0]
    assert (kw["deployment_id"], kw["deploy_strategy"], kw["compute_target"]) == (str(main_id), DeployStrategy.MAIN, ComputeTarget.GPU)
    assert out.id == main_id and out.predict_key is None  # same URL, same key: nothing new is minted
    assert not any(sql.startswith("INSERT INTO deployments") for sql, _ in deploy_env.conn.calls)


def test_a_main_strategy_redeploy_never_resurrects_a_stopped_deployment(deploy_env):
    deploy_env.conn.vals["SELECT settings"] = {SETTING_DEPLOY_STRATEGY: DeployStrategy.MAIN}
    deploy_env.conn.rows["FROM deployments"] = {"id": uuid.uuid4(), "status": "stopped", "predict_key_hash": "h", "rate_limit_per_minute": 10}
    with pytest.raises(HTTPException) as e:
        asyncio.run(service.create_deployment(CTX, PROJECT, RUN))
    assert (e.value.status_code, e.value.code) == (409, ProblemCode.STOPPED) and not deploy_env.deployed


def test_a_main_strategy_redeploy_that_loses_the_race_to_a_stop_is_409(deploy_env):
    deploy_env.conn.vals["SELECT settings"] = {SETTING_DEPLOY_STRATEGY: DeployStrategy.MAIN}
    deploy_env.conn.rows["FROM deployments"] = {"id": uuid.uuid4(), "status": "active", "predict_key_hash": "h", "rate_limit_per_minute": 10}
    deploy_env.conn.rows["UPDATE deployments"] = None  # the UPDATE only touches a row that is not stopped
    with pytest.raises(HTTPException) as e:
        asyncio.run(service.create_deployment(CTX, PROJECT, RUN))
    assert e.value.code == ProblemCode.STOPPED


def test_the_main_lookup_leaves_deleted_rows_out_so_a_new_key_is_minted(deploy_env):
    deploy_env.conn.vals["SELECT settings"] = {SETTING_DEPLOY_STRATEGY: DeployStrategy.MAIN}
    deploy_env.conn.rows["FROM deployments"] = None
    deploy_env.conn.rows["INSERT INTO deployments"] = {"id": uuid.uuid4(), "status": "deploying", "route_prefix": "/p", "rate_limit_per_minute": 10}
    assert asyncio.run(service.create_deployment(CTX, PROJECT, RUN)).predict_key
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
        asyncio.run(service.create_deployment(CTX, PROJECT, RUN))
    assert e.value.status_code == 409 and not deploy_env.deployed


def test_a_detection_run_deploys_with_the_class_names_its_report_lists(deploy_env):
    deploy_env.conn.vals["SELECT task_type"] = TaskType.OBJECT_DETECTION
    deploy_env.conn.rows["FROM training_runs"] = {**SERVABLE, "class_names": ["person", "car"]}
    deploy_env.conn.rows["INSERT INTO deployments"] = {
        "id": uuid.uuid4(), "status": "deploying", "route_prefix": "/predict/x", "rate_limit_per_minute": PLAN_TIER_RATE_LIMITS[PlanTier.PRO],
    }
    asyncio.run(service.create_deployment(CTX, PROJECT, RUN))
    assert deploy_env.deployed[-1]["class_names"] == ["person", "car"]


@pytest.mark.parametrize("task", [None, TaskType.TABULAR_CLASSIFICATION, TaskType.TEXT_GENERATION])
def test_deploy_requires_a_servable_task_whatever_the_report_carries(deploy_env, task):
    deploy_env.conn.vals["SELECT task_type"] = task  # a tabular classifier's report has labels too
    with pytest.raises(HTTPException) as e:
        asyncio.run(service.create_deployment(CTX, PROJECT, RUN))
    assert e.value.status_code == 409 and not deploy_env.deployed


def test_deploy_of_another_projects_run_is_404(deploy_env):
    deploy_env.conn.rows["FROM training_runs"] = None
    with pytest.raises(HTTPException) as e:
        asyncio.run(service.create_deployment(CTX, PROJECT, RUN))
    assert e.value.status_code == 404 and not deploy_env.deployed


def test_try_it_calls_serve_at_the_configured_head(monkeypatch):
    seen = []

    class Client:
        def __init__(self, **kw): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

        async def post(self, url, json):
            seen.append(url)
            return "resp"

    monkeypatch.setattr(service.httpx, "AsyncClient", Client)
    monkeypatch.setattr(service.serve_client, "serve_url", lambda: "http://head:8000")
    resp, _ = asyncio.run(service._post_to_serve("/predict/x", {}))
    assert (resp, seen) == ("resp", ["http://head:8000/predict/x"])


def test_test_deployment_releases_the_connection_before_calling_serve(monkeypatch):
    open_conns = 0

    class Ctx:
        async def __aenter__(self):
            nonlocal open_conns
            open_conns += 1
            return FakeConn(row={"status": "active", "route_prefix": "/p", "serving": "legacy", "training_run_id": uuid.uuid4()})

        async def __aexit__(self, *a):
            nonlocal open_conns
            open_conns -= 1

    monkeypatch.setattr(service, "TenantConn", lambda tenant_id: Ctx())

    class Resp:
        def raise_for_status(self): ...
        def json(self):
            return {"ok": 1}

    async def fake_post(route_prefix, body):
        assert open_conns == 0
        return Resp(), 1.5

    monkeypatch.setattr(service, "_post_to_serve", fake_post)
    out = asyncio.run(service.test_deployment(CTX, uuid.uuid4(), uuid.uuid4(), {}))
    assert (out.response, out.latency_ms) == ({"ok": 1}, 1.5)
