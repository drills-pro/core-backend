"""Stop, start and delete a deployment: moves along the DEPLOYMENTS edges (status.py), audit and outbox, what predict and try-it answer."""
import asyncio
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.authz import Action, Role, TenantStatus
from app.core.problem import ProblemCode as KernelProblemCode
from app.core.settings import settings
from app.modules.user.deployments import predict, routes, service
from app.modules.user.deployments.constants import AuditAction, EntityType, ProblemCode, Serving
from app.modules.user.deployments.status import DeploymentStatus as S
from tests.support import PROJECT, FakeConn, TenantCtx, access, make_client, patch_tenant_conn

CTX = SimpleNamespace(tenant_id=str(uuid.uuid4()), actor="u1")
DEPLOYMENT, RUN = uuid.uuid4(), uuid.uuid4()
BASE = f"/v1/projects/{PROJECT}/deployments/{DEPLOYMENT}"
UPDATE, SELECT = "UPDATE deployments", "FROM deployments WHERE"  # keys into FakeConn.rows


def row(status, serving=Serving.SHARED, route_prefix=f"/predict/{DEPLOYMENT}"):
    return {
        "id": DEPLOYMENT, "tenant_id": CTX.tenant_id, "project_id": PROJECT, "training_run_id": RUN, "model_version_id": None, "status": status, "route_prefix": route_prefix,
        "rate_limit_per_minute": 10, "created_at": datetime.now(timezone.utc), "serving": serving,
    }


@pytest.fixture
def env(monkeypatch):
    e = SimpleNamespace(conn=FakeConn(vals={"count(*) FROM deployments": 0}))
    patch_tenant_conn(monkeypatch, e.conn, service)
    return e


def moves(env):
    """The args of every status UPDATE the service issued."""
    return [a for sql, a in env.conn.calls if sql.startswith("UPDATE deployments SET status")]


AUDIT_ARGS, EVENT_ARGS = 6, 5  # audit.record: tenant, project, actor, action, entity_type, entity_id; outbox: tenant, project, kind, entity, status


def audited(env):
    return [(a[3], a[4], a[5]) for a in env.conn.executed if len(a) == AUDIT_ARGS]


def events(env):
    return [(a[2], a[4]) for a in env.conn.executed if len(a) == EVENT_ARGS]


def run(coro):
    return asyncio.run(coro)


def test_stop_moves_active_to_stopped_with_an_audit_row_and_keeps_the_key(env):
    env.conn.rows = {UPDATE: row(S.STOPPED), SELECT: row(S.ACTIVE)}
    out = run(service.stop_deployment(CTX, PROJECT, DEPLOYMENT))
    assert out.status == S.STOPPED
    (args,) = moves(env)
    assert args == (DEPLOYMENT, S.ACTIVE, S.STOPPED)  # compare-and-set on the status it was read at; the key is not cleared
    assert any(sql.endswith("FOR UPDATE") for sql, _ in env.conn.calls)  # the row is locked from the read to the move
    assert audited(env) == [(AuditAction.STOP, EntityType.DEPLOYMENT, str(DEPLOYMENT))]
    assert events(env) == [("deployment.stopped", "stopped")]


def test_stop_of_a_stopped_deployment_is_a_repeat_not_an_error_and_writes_no_audit(env):
    env.conn.rows = {UPDATE: None, SELECT: row(S.STOPPED)}
    out = run(service.stop_deployment(CTX, PROJECT, DEPLOYMENT))
    assert out.status == S.STOPPED and audited(env) == [] and moves(env) == []


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


@pytest.mark.parametrize("current", [S.ACTIVE, S.STOPPED, S.FAILED, S.DEPLOYING])
def test_delete_clears_the_predict_key_and_audits(env, current):
    env.conn.rows = {UPDATE: row(S.DELETED), SELECT: row(current, Serving.LEGACY)}  # a leftover legacy row has nothing of its own to remove
    run(service.delete_deployment(CTX, PROJECT, DEPLOYMENT))
    sql, args = next((s, a) for s, a in env.conn.calls if s.startswith("UPDATE deployments SET status"))
    assert "predict_key_hash = $4" in sql and args == (DEPLOYMENT, current, S.DELETED, None)
    assert audited(env) == [(AuditAction.DELETE, EntityType.DEPLOYMENT, str(DEPLOYMENT))]


@pytest.mark.parametrize("serving", list(Serving))
def test_start_is_active_again_on_the_shared_apps_even_for_a_leftover_legacy_row(env, serving):
    env.conn.rows = {UPDATE: row(S.ACTIVE, serving), SELECT: row(S.STOPPED, serving)}
    out = run(service.start_deployment(CTX, PROJECT, DEPLOYMENT))
    (args,) = moves(env)
    assert (out.status, args) == (S.ACTIVE, (DEPLOYMENT, S.STOPPED, S.ACTIVE))
    assert audited(env) == [(AuditAction.START, EntityType.DEPLOYMENT, str(DEPLOYMENT))]


def test_a_repeated_start_does_not_deploy_again(env):
    env.conn.rows = {UPDATE: None, SELECT: row(S.ACTIVE)}
    assert run(service.start_deployment(CTX, PROJECT, DEPLOYMENT)).status == S.ACTIVE
    assert audited(env) == [] and moves(env) == []


def test_start_of_a_failed_deployment_is_409(env):
    env.conn.rows = {UPDATE: row(S.ACTIVE), SELECT: row(S.FAILED)}
    with pytest.raises(HTTPException) as e:
        run(service.start_deployment(CTX, PROJECT, DEPLOYMENT))
    assert (e.value.status_code, e.value.code) == (409, ProblemCode.INVALID_TRANSITION) and moves(env) == []


# ---- what the rest of the surface answers ----

def test_a_stopped_deployment_answers_409_with_its_own_code():
    with pytest.raises(HTTPException) as stopped:
        predict.require_active(S.STOPPED)
    with pytest.raises(HTTPException) as failed:
        predict.require_active(S.FAILED)
    predict.require_active(S.ACTIVE)
    assert (stopped.value.status_code, stopped.value.code) == (409, ProblemCode.STOPPED)
    assert (failed.value.status_code, failed.value.code) == (409, ProblemCode.NOT_ACTIVE)


def test_predict_on_a_stopped_deployment_is_409_and_its_key_is_still_looked_up(monkeypatch):
    conn = FakeConn(row={"route_prefix": "/p", "status": S.STOPPED, "rate_limit_per_minute": 10, "tenant_id": uuid.uuid4(), "project_id": PROJECT, "tenant_status": TenantStatus.ACTIVE})

    async def pool():
        return SimpleNamespace(acquire=lambda: TenantCtx(conn))

    monkeypatch.setattr(predict, "get_pool", pool)
    with pytest.raises(HTTPException) as e:
        run(predict.predict(DEPLOYMENT, "key", {}))
    assert (e.value.status_code, e.value.code) == (409, ProblemCode.STOPPED)
    assert conn.calls[0][1][1] == predict.hash_key("key")  # the stored hash is untouched, so the same key matches


@pytest.mark.parametrize("tenant_status", [TenantStatus.SUSPENDED, TenantStatus.PENDING_DELETE])
def test_predict_for_a_tenant_that_is_not_active_is_503_before_serve(monkeypatch, tenant_status):
    conn = FakeConn(row={"route_prefix": "/p", "status": S.ACTIVE, "rate_limit_per_minute": 10, "tenant_id": uuid.uuid4(), "project_id": PROJECT, "tenant_status": tenant_status})

    async def pool():
        return SimpleNamespace(acquire=lambda: TenantCtx(conn))

    async def serve_must_not_run(*_):
        raise AssertionError("Serve was called")

    monkeypatch.setattr(predict, "get_pool", pool)
    monkeypatch.setattr(predict, "call", serve_must_not_run)
    with pytest.raises(HTTPException) as e:
        run(predict.predict(DEPLOYMENT, "key", {}))
    assert (e.value.status_code, e.value.code) == (503, KernelProblemCode.TENANT_SUSPENDED)


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
        patch_tenant_conn(monkeypatch, FakeConn(acc, row=None, vals={"count(*) FROM deployments": 0}), service)
        return getattr(client, method)(path)

    return go


@pytest.mark.parametrize(("method", "path"), [("post", f"{BASE}/stop"), ("post", f"{BASE}/start"), ("delete", BASE)])
def test_only_a_manager_may_change_a_deployments_state(call, method, path):
    r = call(method, path, access())
    assert (r.status_code, r.json()["action"]) == (403, Action.DEPLOYMENT_MANAGE)
    assert call(method, path, access(roles=[Role.EDITOR])).status_code == 403
    assert call(method, path, access(roles=[Role.DEPLOYER])).status_code == 404  # authorized; the fake database has no such row


def test_start_takes_a_slot_back_so_it_is_429_when_the_plan_is_full(env):
    env.conn.rows = {UPDATE: row(S.ACTIVE), SELECT: row(S.STOPPED)}
    env.conn.vals["count(*) FROM deployments"] = settings.serving.max_active_deployments_per_tenant
    with pytest.raises(HTTPException) as e:
        run(service.start_deployment(CTX, PROJECT, DEPLOYMENT))
    assert (e.value.status_code, e.value.code) == (429, ProblemCode.TOO_MANY_DEPLOYMENTS) and moves(env) == []


def test_stop_and_delete_do_not_check_the_quota(env):
    env.conn.vals["count(*) FROM deployments"] = 10**6
    env.conn.rows = {UPDATE: row(S.STOPPED), SELECT: row(S.ACTIVE)}
    run(service.stop_deployment(CTX, PROJECT, DEPLOYMENT))
    env.conn.rows = {UPDATE: row(S.DELETED), SELECT: row(S.STOPPED)}
    run(service.delete_deployment(CTX, PROJECT, DEPLOYMENT))
