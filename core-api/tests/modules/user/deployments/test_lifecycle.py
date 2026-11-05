"""Stop, start and delete a deployment: compare-and-set transitions, audit, Serve app removal, what predict and try-it answer."""
import asyncio
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.authz import Action, Role
from app.modules.user.deployments import routes, service
from app.modules.user.deployments.constants import AuditAction, DeployStrategy, EntityType, ProblemCode, Serving
from app.modules.user.deployments.status import DeploymentStatus as S
from app.modules.user.training import TrainingStatus
from tests.support import PROJECT, FakeConn, TenantCtx, access, make_client, patch_tenant_conn

CTX = SimpleNamespace(tenant_id=str(uuid.uuid4()), actor="u1")
DEPLOYMENT, RUN = uuid.uuid4(), uuid.uuid4()
BASE = f"/v1/projects/{PROJECT}/deployments/{DEPLOYMENT}"
UPDATE, SELECT = "UPDATE deployments", "FROM deployments WHERE"  # keys into FakeConn.rows


def row(status, serving=Serving.SHARED, route_prefix=f"/predict/{DEPLOYMENT}"):
    return {
        "id": DEPLOYMENT, "project_id": PROJECT, "training_run_id": RUN, "status": status, "route_prefix": route_prefix,
        "rate_limit_per_minute": 10, "created_at": datetime.now(timezone.utc), "serving": serving,
    }


@pytest.fixture
def env(monkeypatch):
    class Env:
        conn = FakeConn()
        removed, deployed, stopped_jobs = [], [], []

    e = Env()
    patch_tenant_conn(monkeypatch, e.conn, service)

    async def remove(**kw):
        e.removed.append(kw)

    async def deploy(**kw):
        e.deployed.append(kw)
        return "ray-job-2", kw["deployment_id"]

    async def stop_quietly(job_id):
        e.stopped_jobs.append(job_id)

    monkeypatch.setattr(service.serve_client, "remove", remove)
    monkeypatch.setattr(service.serve_client, "deploy", deploy)
    monkeypatch.setattr(service.job_lifecycle, "stop_quietly", stop_quietly)
    return e


def moves(env):
    """The args of every status UPDATE the service issued."""
    return [a for sql, a in env.conn.calls if sql.startswith("UPDATE deployments SET status")]


def audited(env):
    return [(a[3], a[4], a[5]) for a in env.conn.executed]


def run(coro):
    return asyncio.run(coro)


def test_stop_moves_active_to_stopped_with_an_audit_row_and_keeps_the_key(env):
    env.conn.rows = {UPDATE: row(S.STOPPED)}
    out = run(service.stop_deployment(CTX, PROJECT, DEPLOYMENT))
    assert out.status == S.STOPPED
    (args,) = moves(env)
    assert (args[2], args[3], args[5]) == (S.STOPPED, [S.ACTIVE], False)  # only an active one; the key is not cleared
    assert audited(env) == [(AuditAction.STOP, EntityType.DEPLOYMENT, str(DEPLOYMENT))]
    assert env.removed == []  # shared serving: nothing to remove


def test_stop_removes_the_serve_app_of_a_legacy_deployment(env):
    env.conn.rows = {UPDATE: row(S.STOPPED, Serving.LEGACY)}
    run(service.stop_deployment(CTX, PROJECT, DEPLOYMENT))
    assert env.removed == [dict(deployment_id=str(DEPLOYMENT), project_id=str(PROJECT), tenant_id=CTX.tenant_id, deploy_strategy=DeployStrategy.LATEST)]


def test_a_main_legacy_deployment_removes_the_projects_main_app(env):
    env.conn.rows = {UPDATE: row(S.STOPPED, Serving.LEGACY, f"/predict/{PROJECT}/main")}
    run(service.stop_deployment(CTX, PROJECT, DEPLOYMENT))
    assert env.removed[0]["deploy_strategy"] == DeployStrategy.MAIN


def test_stop_of_a_stopped_deployment_is_a_repeat_not_an_error_and_writes_no_audit(env):
    env.conn.rows = {UPDATE: None, SELECT: row(S.STOPPED, Serving.LEGACY)}
    out = run(service.stop_deployment(CTX, PROJECT, DEPLOYMENT))
    assert out.status == S.STOPPED and audited(env) == []
    assert len(env.removed) == 1  # a removal that failed the first time is retried


@pytest.mark.parametrize("current", [S.DEPLOYING, S.FAILED])
def test_stop_of_a_deployment_that_is_not_active_is_409(env, current):
    env.conn.rows = {UPDATE: None, SELECT: row(current)}
    with pytest.raises(HTTPException) as e:
        run(service.stop_deployment(CTX, PROJECT, DEPLOYMENT))
    assert (e.value.status_code, e.value.code) == (409, ProblemCode.INVALID_TRANSITION) and audited(env) == []


def test_a_missing_or_deleted_deployment_is_404(env):
    env.conn.rows = {UPDATE: None, SELECT: None}  # the SELECT leaves deleted rows out
    for call in (service.stop_deployment, service.start_deployment, service.delete_deployment):
        with pytest.raises(HTTPException) as e:
            run(call(CTX, PROJECT, DEPLOYMENT))
        assert e.value.status_code == 404


def test_delete_clears_the_predict_key_and_audits(env):
    env.conn.rows = {UPDATE: row(S.DELETED, Serving.LEGACY)}
    run(service.delete_deployment(CTX, PROJECT, DEPLOYMENT))
    sql, args = next((s, a) for s, a in env.conn.calls if s.startswith("UPDATE deployments SET status"))
    assert "predict_key_hash = CASE WHEN $6 THEN NULL" in sql
    assert (args[2], args[3], args[5]) == (S.DELETED, [S.ACTIVE, S.STOPPED, S.FAILED], True)  # never from deploying
    assert audited(env) == [(AuditAction.DELETE, EntityType.DEPLOYMENT, str(DEPLOYMENT))]
    assert len(env.removed) == 1


def test_start_of_a_shared_deployment_is_active_again_without_a_deploy(env):
    env.conn.rows = {UPDATE: row(S.ACTIVE), SELECT: row(S.STOPPED)}
    out = run(service.start_deployment(CTX, PROJECT, DEPLOYMENT))
    (args,) = moves(env)
    assert (out.status, args[2], args[3], args[4], args[5]) == (S.ACTIVE, S.ACTIVE, [S.STOPPED], None, False)
    assert env.deployed == [] and audited(env) == [(AuditAction.START, EntityType.DEPLOYMENT, str(DEPLOYMENT))]


def test_start_of_a_legacy_deployment_redeploys_it_and_waits_for_the_reconciler(env, monkeypatch):
    async def servable_run(conn, project_id, run_id):
        return SimpleNamespace(status=TrainingStatus.SUCCEEDED, mlflow_run_id="m1", class_names=["a"], servable=True)

    async def task(conn, project_id):
        return "image-classification"

    async def settings(conn, project_id):
        return {}

    async def info(conn, task):
        return SimpleNamespace(enabled=True, servable=True)

    monkeypatch.setattr(service, "get_run_model", servable_run)
    monkeypatch.setattr(service, "get_task_type", task)
    monkeypatch.setattr(service, "get_settings", settings)
    monkeypatch.setattr(service.task_types, "get", info)
    env.conn.rows = {UPDATE: row(S.DEPLOYING, Serving.LEGACY), SELECT: row(S.STOPPED, Serving.LEGACY)}
    out = run(service.start_deployment(CTX, PROJECT, DEPLOYMENT))
    (args,) = moves(env)
    assert (out.status, args[2], args[4]) == (S.DEPLOYING, S.DEPLOYING, "ray-job-2")
    assert env.deployed[0]["deployment_id"] == str(DEPLOYMENT) and env.stopped_jobs == []


def test_a_repeated_start_does_not_deploy_again(env):
    env.conn.rows = {UPDATE: None, SELECT: row(S.ACTIVE)}
    assert run(service.start_deployment(CTX, PROJECT, DEPLOYMENT)).status == S.ACTIVE
    assert audited(env) == [] and env.deployed == []


def test_start_of_a_failed_deployment_is_409(env):
    env.conn.rows = {UPDATE: None, SELECT: row(S.FAILED)}
    with pytest.raises(HTTPException) as e:
        run(service.start_deployment(CTX, PROJECT, DEPLOYMENT))
    assert e.value.status_code == 409


# ---- what the rest of the surface answers ----

def test_a_stopped_deployment_answers_409_with_its_own_code():
    with pytest.raises(HTTPException) as stopped:
        service._require_active(S.STOPPED)
    with pytest.raises(HTTPException) as failed:
        service._require_active(S.FAILED)
    service._require_active(S.ACTIVE)
    assert (stopped.value.status_code, stopped.value.code) == (409, ProblemCode.STOPPED)
    assert (failed.value.status_code, failed.value.code) == (409, ProblemCode.NOT_ACTIVE)


def test_try_it_on_a_stopped_deployment_is_409_and_on_a_deleted_one_404(env):
    env.conn.row = {"status": S.STOPPED, "route_prefix": "/p", "serving": Serving.SHARED, "training_run_id": RUN}
    with pytest.raises(HTTPException) as e:
        run(service.test_deployment(CTX, PROJECT, DEPLOYMENT, {}))
    assert (e.value.status_code, e.value.code) == (409, ProblemCode.STOPPED)
    env.conn.row = None  # the query leaves deleted rows out
    with pytest.raises(HTTPException) as e:
        run(service.test_deployment(CTX, PROJECT, DEPLOYMENT, {}))
    assert e.value.status_code == 404


def test_predict_on_a_stopped_deployment_is_409_and_its_key_is_still_looked_up(monkeypatch):
    conn = FakeConn(row={"route_prefix": "/p", "status": S.STOPPED, "rate_limit_per_minute": 10, "tenant_id": uuid.uuid4(), "project_id": PROJECT})

    async def pool():
        return SimpleNamespace(acquire=lambda: TenantCtx(conn))

    monkeypatch.setattr(service, "get_pool", pool)
    with pytest.raises(HTTPException) as e:
        run(service.predict(DEPLOYMENT, "key", {}))
    assert (e.value.status_code, e.value.code) == (409, ProblemCode.STOPPED)
    assert conn.calls[0][1][1] == service.hash_key("key")  # the stored hash is untouched, so the same key matches


def test_list_get_and_test_calls_leave_deleted_rows_out(env):
    run(service.list_deployments(CTX, PROJECT))
    env.conn.row, env.conn.val = None, None
    with pytest.raises(HTTPException):
        run(service.get_deployment(CTX, PROJECT, DEPLOYMENT))
    with pytest.raises(HTTPException):
        run(service.list_test_calls(CTX, PROJECT, DEPLOYMENT, 5))
    deployments_queries = [args for sql, args in env.conn.calls if "FROM deployments" in sql]
    assert len(deployments_queries) == 3 and all(S.DELETED in args for args in deployments_queries)


# ---- routes ----

@pytest.fixture
def call(monkeypatch, dev_headers):
    client = make_client(routes.router, raise_server_exceptions=False)

    def go(method, path, acc):
        patch_tenant_conn(monkeypatch, FakeConn(acc, row=None), service)
        return getattr(client, method)(path)

    return go


@pytest.mark.parametrize(("method", "path"), [("post", f"{BASE}/stop"), ("post", f"{BASE}/start"), ("delete", BASE)])
def test_only_a_manager_may_change_a_deployments_state(call, method, path):
    r = call(method, path, access())
    assert (r.status_code, r.json()["action"]) == (403, Action.DEPLOYMENT_MANAGE)
    assert call(method, path, access(roles=[Role.EDITOR])).status_code == 403
    assert call(method, path, access(roles=[Role.DEPLOYER])).status_code == 404  # authorized; the fake database has no such row
