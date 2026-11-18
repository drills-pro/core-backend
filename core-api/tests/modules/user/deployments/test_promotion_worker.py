"""The promotion worker: deploy the version, flip the stages only when that deployment is active, and on any failure leave every stage
as it was with the error on the version (swap only on success, planning/27)."""
import asyncio
import uuid

import pytest

from app.core.problem import Problem
from app.modules.user.deployments import promotion_worker as worker
from app.modules.user.deployments.constants import PROMOTION_FAILED_DETAIL
from tests.support import FakeConn
from tests.support.counted_conn import patch_counted_conn

TENANT, PROJECT, VERSION, SERVING = str(uuid.uuid4()), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
ROW = {"id": VERSION, "project_id": PROJECT, "promotion_actor": "user-1", "promotion_acknowledged_risk": True}


@pytest.fixture
def env(monkeypatch):
    e = type("Env", (), {})()
    e.steps, e.serving = [], SERVING
    e.conn = FakeConn()
    patch_counted_conn(monkeypatch, e.conn, worker)

    async def serving_version(conn, version_id):
        return e.serving

    async def ensure_deployed(ctx, project_id, version_id, acknowledge_risk=False):
        e.steps.append(("deploy", version_id, acknowledge_risk, ctx.actor))

    async def complete(ctx, project_id, version_id):
        e.steps.append(("flip", version_id))

    async def stop(ctx, project_id, version_id):
        e.steps.append(("stop", version_id))

    async def fail(ctx, project_id, version_id, error, code=None):
        e.steps.append(("fail", version_id, error, code))

    monkeypatch.setattr(worker, "serving_version", serving_version)
    monkeypatch.setattr(worker.service, "ensure_deployed", ensure_deployed)
    monkeypatch.setattr(worker, "complete_promotion", complete)
    monkeypatch.setattr(worker, "fail_promotion", fail)
    monkeypatch.setattr(worker.service, "stop_version_deployments", stop)
    return e


def settle(row=ROW):
    asyncio.run(worker._settle(TENANT, row))


def test_the_stage_flips_only_after_the_version_is_deployed(env):
    settle()
    assert env.steps == [("deploy", VERSION, True, "user-1"), ("flip", VERSION), ("stop", SERVING)]  # the actor and the risk answer travel with the request


def test_the_replaced_versions_deployment_is_stopped_only_after_the_flip(env):
    settle()
    assert [s[0] for s in env.steps] == ["deploy", "flip", "stop"] and env.steps[-1] == ("stop", SERVING)


def test_the_first_promotion_of_a_model_stops_nothing(env):
    env.serving = None
    settle()
    assert [s[0] for s in env.steps] == ["deploy", "flip"]


def test_a_failure_stopping_the_old_deployment_is_not_a_failed_promotion(env, monkeypatch):
    async def boom(ctx, project_id, version_id):
        raise RuntimeError("db down")

    monkeypatch.setattr(worker.service, "stop_version_deployments", boom)
    settle()
    assert [s[0] for s in env.steps] == ["deploy", "flip"]  # the stages flipped: no fail_promotion


def test_a_failed_deploy_never_flips_and_shows_the_problems_own_words(env, monkeypatch):
    async def refuse(ctx, project_id, version_id, acknowledge_risk=False):
        env.steps.append(("deploy", version_id))
        if version_id == VERSION:
            raise Problem(429, "too_many_deployments", "too many active deployments for this plan, stop or delete one first")

    monkeypatch.setattr(worker.service, "ensure_deployed", refuse)
    settle()
    assert ("stop", SERVING) not in env.steps  # the old version is left active
    assert env.steps == [
        ("deploy", VERSION), ("deploy", SERVING),  # the version that was serving is deployed again (a no-op unless `main` moved)
        ("fail", VERSION, "too many active deployments for this plan, stop or delete one first", "too_many_deployments")]  # the code lets the client branch


def test_a_failed_flip_puts_the_main_deployment_back_and_hides_internal_errors(env, monkeypatch):
    async def boom(ctx, project_id, version_id):
        raise RuntimeError("mlflow says: secret internals")

    monkeypatch.setattr(worker, "complete_promotion", boom)
    settle()
    assert [s[:2] for s in env.steps] == [("deploy", VERSION), ("deploy", SERVING), ("fail", VERSION)]
    assert env.steps[-1][2:] == (PROMOTION_FAILED_DETAIL, None)


def test_the_first_promotion_of_a_model_has_nothing_to_put_back(env, monkeypatch):
    env.serving = None

    async def boom(ctx, project_id, version_id):
        raise RuntimeError("x")

    monkeypatch.setattr(worker, "complete_promotion", boom)
    settle()
    assert [s[0] for s in env.steps] == ["deploy", "fail"]


def test_putting_the_deployment_back_never_hides_the_real_failure(env, monkeypatch):
    async def deploy(ctx, project_id, version_id, acknowledge_risk=False):
        if version_id == SERVING:
            raise RuntimeError("cannot")
        raise Problem(409, "not_deployable", "no deployable model")

    monkeypatch.setattr(worker.service, "ensure_deployed", deploy)
    settle()
    assert env.steps == [("fail", VERSION, "no deployable model", "not_deployable")]


def test_one_failing_promotion_does_not_stop_the_next(env, monkeypatch):
    other = {**ROW, "id": uuid.uuid4()}
    pending = [ROW, other]

    async def tenants(sql):
        return [(uuid.UUID(TENANT),)]

    async def pending_promotions(conn, tenant_id):
        return pending

    calls = []

    async def settle_one(tenant_id, row):
        calls.append(row["id"])
        if row is ROW:
            raise RuntimeError("could not even mark it failed")

    monkeypatch.setattr(worker.job_lifecycle, "fetch", tenants)
    monkeypatch.setattr(worker, "pending_promotions", pending_promotions)
    monkeypatch.setattr(worker, "_settle", settle_one)
    asyncio.run(worker.process_promotions())
    assert calls == [ROW["id"], other["id"]]


def test_the_worker_is_polled():
    from app import background

    assert any(fn is worker.process_promotions for _, fn, _ in background.POLLERS)


# ---- with the real deployment service over a fake database: what a swap and a rollback do to the deployments ----

def test_a_swap_stops_the_demoted_versions_active_deployment_and_a_rollback_starts_the_stopped_one_it_returns_to(monkeypatch):
    from app.modules.user.deployments import service
    from app.modules.user.deployments.status import DeploymentStatus as S

    old, new = uuid.uuid4(), uuid.uuid4()
    d_old, d_new = uuid.uuid4(), uuid.uuid4()
    rows = {d_old: {"version": old, "status": S.ACTIVE}, d_new: {"version": new, "status": S.STOPPED}}  # rolling back to `new`, which was stopped
    moves = []

    class Conn(FakeConn):
        async def fetchrow(self, sql, *args):
            if "FROM deployments WHERE model_version_id" in sql:
                found = [(i, r) for i, r in rows.items() if r["version"] == args[0] and r["status"] in args[1]]
                return {"id": found[0][0], "status": found[0][1]["status"]} if found else None
            return await super().fetchrow(sql, *args)

        async def fetch(self, sql, *args):
            return [{"id": i} for i, r in rows.items() if r["version"] == args[0] and r["status"] == args[1]]

    conn = Conn()
    patch_counted_conn(monkeypatch, conn, service)

    async def move(ctx, project_id, deployment_id, *, to, action, only_from=None):
        rows[deployment_id]["status"] = to
        moves.append((deployment_id, to, action))
        return {}, True

    async def start(ctx, project_id, deployment_id):
        await move(ctx, project_id, deployment_id, to=S.ACTIVE, action="start")

    async def flip(ctx, project_id, version_id):
        moves.append(("flip", version_id))

    async def serving_version(conn_, version_id):
        return old

    monkeypatch.setattr(service, "_move", move)
    monkeypatch.setattr(service, "start_deployment", start)
    monkeypatch.setattr(worker, "complete_promotion", flip)
    monkeypatch.setattr(worker, "serving_version", serving_version)
    patch_counted_conn(monkeypatch, conn, worker)
    asyncio.run(worker._settle(TENANT, {**ROW, "id": new}))
    assert moves == [(d_new, S.ACTIVE, "start"), ("flip", new), (d_old, S.STOPPED, "stop")]  # started first, the old one stopped last
    assert (rows[d_new]["status"], rows[d_old]["status"]) == (S.ACTIVE, S.STOPPED)  # one version live, and the stopped row is kept
