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

    async def fail(ctx, project_id, version_id, error, code=None):
        e.steps.append(("fail", version_id, error, code))

    monkeypatch.setattr(worker, "serving_version", serving_version)
    monkeypatch.setattr(worker.service, "ensure_deployed", ensure_deployed)
    monkeypatch.setattr(worker, "complete_promotion", complete)
    monkeypatch.setattr(worker, "fail_promotion", fail)
    return e


def settle(row=ROW):
    asyncio.run(worker._settle(TENANT, row))


def test_the_stage_flips_only_after_the_version_is_deployed(env):
    settle()
    assert env.steps == [("deploy", VERSION, True, "user-1"), ("flip", VERSION)]  # the actor and the risk answer travel with the request


def test_a_failed_deploy_never_flips_and_shows_the_problems_own_words(env, monkeypatch):
    async def refuse(ctx, project_id, version_id, acknowledge_risk=False):
        env.steps.append(("deploy", version_id))
        if version_id == VERSION:
            raise Problem(429, "too_many_deployments", "too many active deployments for this plan, stop or delete one first")

    monkeypatch.setattr(worker.service, "ensure_deployed", refuse)
    settle()
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
