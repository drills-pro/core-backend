"""Creating a deployment: Idempotency-Key replay, one live deployment per run (unique index -> 409), the plan's active quota."""
import asyncio
import re
import uuid
from pathlib import Path
from types import SimpleNamespace

import asyncpg
import pytest
from fastapi import HTTPException

from app.core.enums import TaskType
from app.core.settings import settings
from app.modules.user.deployments import routes, service
from app.modules.user.deployments.constants import SETTING_DEPLOY_STRATEGY, AuditAction, DeployStrategy, EntityType, ProblemCode
from app.modules.user.deployments.status import LIVE_STATUSES, QUOTA_STATUSES, DeploymentStatus
from app.modules.user.training import TrainingStatus
from tests.support import PROJECT, FakeConn, access, make_client, patch_tenant_conn

MIGRATION = next((Path(__file__).parents[4] / "migrations").glob("*_deployments_quota_idempotency.sql")).read_text().split("-- migrate:down")[0]
CTX = SimpleNamespace(tenant_id=str(uuid.uuid4()), actor="user-1")
RUN, OTHER_RUN, KEY = uuid.uuid4(), uuid.uuid4(), "key-1"
SERVABLE = {"id": RUN, "status": TrainingStatus.SUCCEEDED, "mlflow_run_id": "mlflow-1", "class_names": ["cat"], "test_dataset_id": uuid.uuid4()}
CREATED = {"id": uuid.uuid4(), "status": "active", "route_prefix": "/predict/x", "rate_limit_per_minute": 10, "training_run_id": RUN}


class RaisingConn(FakeConn):
    """A FakeConn whose statements that start with one of `fail` hit the unique index."""

    def __init__(self, *a, fail=(), **kw):
        super().__init__(*a, **kw)
        self.fail = fail

    async def fetchrow(self, sql, *args):
        if sql.startswith(self.fail):
            raise asyncpg.UniqueViolationError("duplicate key value violates unique constraint")
        return await super().fetchrow(sql, *args)


@pytest.fixture
def env(monkeypatch):
    class Env:
        removed = []
        deployed = []
        shared = True

    e = Env()
    e.conn = RaisingConn(
        rows={"idempotency_key = $2": None, "FROM training_runs": SERVABLE, "INSERT INTO deployments": CREATED},
        vals={"count(*) FROM deployments": 0, "SELECT settings": {}, "SELECT plan_tier": "pro", "SELECT task_type": TaskType.IMAGE_CLASSIFICATION})
    patch_tenant_conn(monkeypatch, e.conn, service)

    async def apps_declared():
        return e.shared

    async def deploy(**kw):
        e.deployed.append(kw)
        return "ray-job-1", f"/predict/{kw['deployment_id']}"

    async def remove(**kw):
        e.removed.append(kw)

    monkeypatch.setattr(service.serve_client, "apps_declared", apps_declared)
    monkeypatch.setattr(service.serve_client, "deploy", deploy)
    monkeypatch.setattr(service.serve_client, "remove", remove)
    return e


def create(run=RUN, key=KEY):
    return asyncio.run(service.create_deployment(CTX, PROJECT, run, key))


def test_the_same_key_and_run_replays_the_deployment_without_the_key_or_a_deploy(env):
    env.conn.rows["idempotency_key = $2"] = CREATED
    out = create()
    assert out.id == CREATED["id"] and out.predict_key is None and not env.deployed
    assert not any(sql.startswith("INSERT") for sql, _ in env.conn.calls)


def test_the_same_key_with_another_run_is_409(env):
    env.conn.rows["idempotency_key = $2"] = CREATED
    with pytest.raises(HTTPException) as e:
        create(run=OTHER_RUN)
    assert (e.value.status_code, e.value.code) == (409, ProblemCode.IDEMPOTENCY_MISMATCH)


def test_a_create_that_loses_the_race_for_the_same_key_returns_the_winner(env):
    calls = iter([None, CREATED])  # the by-key read before the insert, then after it did nothing
    env.conn.rows = {"FROM training_runs": SERVABLE, "INSERT INTO deployments": None}

    async def by_key(sql, *args):
        return next(calls) if sql.startswith("SELECT * FROM deployments") else await FakeConn.fetchrow(env.conn, sql, *args)

    env.conn.fetchrow = by_key
    out = create()
    assert out.id == CREATED["id"] and out.predict_key is None


def test_the_key_is_stored_with_the_new_deployment_and_the_insert_skips_a_taken_key(env):
    out = create()
    sql, args = next((s, a) for s, a in env.conn.calls if s.startswith("INSERT INTO deployments"))
    assert args[-2] == KEY and "ON CONFLICT (project_id, idempotency_key)" in sql and out.predict_key


def test_a_new_deployment_writes_a_create_audit_row(env):
    create()
    audits = [a for s, a in env.conn.calls if s.startswith("INSERT INTO audit_log")]
    assert [a[3:5] for a in audits] == [(AuditAction.CREATE, EntityType.DEPLOYMENT)]


def test_a_second_live_deployment_of_the_run_is_409_deployment_exists(env):
    env.conn.fail = ("INSERT INTO deployments",)
    with pytest.raises(HTTPException) as e:
        create()
    assert (e.value.status_code, e.value.code) == (409, ProblemCode.DEPLOYMENT_EXISTS)


def test_a_main_redeploy_onto_a_run_another_deployment_holds_is_409(env):
    env.conn.vals["SELECT settings"] = {SETTING_DEPLOY_STRATEGY: DeployStrategy.MAIN}
    env.conn.rows["FROM deployments WHERE project_id = $1 AND route_prefix"] = {"id": uuid.uuid4(), "status": "active"}
    env.conn.fail = ("UPDATE deployments",)
    with pytest.raises(HTTPException) as e:
        create()
    assert e.value.code == ProblemCode.DEPLOYMENT_EXISTS


def test_a_legacy_create_that_loses_to_the_index_removes_its_own_serve_app(env):
    env.shared = False
    env.conn.fail = ("INSERT INTO deployments",)
    with pytest.raises(HTTPException):
        create()
    assert [r["deployment_id"] for r in env.removed] == [env.deployed[0]["deployment_id"]]


def test_a_main_legacy_create_that_loses_leaves_the_shared_main_app_alone(env):
    env.shared = False
    env.conn.vals["SELECT settings"] = {SETTING_DEPLOY_STRATEGY: DeployStrategy.MAIN}
    env.conn.fail = ("INSERT INTO deployments",)
    with pytest.raises(HTTPException):
        create()
    assert env.removed == []


def test_quota_boundary_one_below_passes_and_at_the_limit_is_429_before_any_deploy(env):
    env.shared = False
    limit = settings.serving.max_active_deployments_per_tenant  # the YAML fallback applies: no plan_limits row in the fake
    env.conn.vals["count(*) FROM deployments"] = limit - 1
    assert create().predict_key
    env.deployed.clear()
    env.conn.vals["count(*) FROM deployments"] = limit
    with pytest.raises(HTTPException) as e:
        create(key="key-2")
    assert (e.value.status_code, e.value.code) == (429, ProblemCode.TOO_MANY_DEPLOYMENTS) and not env.deployed


def test_the_plan_row_sets_the_limit(env):
    env.conn.rows["FROM plan_limits"] = _plan(2)
    env.conn.vals["count(*) FROM deployments"] = 2
    with pytest.raises(HTTPException) as e:
        create()
    assert e.value.code == ProblemCode.TOO_MANY_DEPLOYMENTS


def test_the_count_is_of_active_and_deploying_only_so_stopped_and_deleted_free_the_slot(env):
    create()
    (_, args), = [c for c in env.conn.calls if "count(*) FROM deployments" in c[0]][:1]
    assert set(args[2]) == {DeploymentStatus.ACTIVE, DeploymentStatus.DEPLOYING} and not {"stopped", "deleted", "failed"} & set(args[2])


def test_the_write_checks_the_quota_under_the_tenants_advisory_lock(env):
    create()
    sqls = [s for s, _ in env.conn.calls]
    lock = next(i for i, s in enumerate(sqls) if "pg_advisory_xact_lock" in s)
    last_count = max(i for i, s in enumerate(sqls) if "count(*) FROM deployments" in s)
    insert = next(i for i, s in enumerate(sqls) if s.startswith("INSERT INTO deployments"))
    assert lock < last_count < insert


def test_the_index_predicate_and_the_quota_statuses_mirror_the_code():
    index = re.search(r"deployments_one_live_per_run ON deployments\(training_run_id\)\s+WHERE status IN \(([^)]*)\)", MIGRATION).group(1)
    assert set(re.findall(r"'(\w+)'", index)) == {s.value for s in LIVE_STATUSES}
    assert set(QUOTA_STATUSES) <= set(LIVE_STATUSES)  # a slot is only ever taken by a live deployment
    assert set(re.findall(r"'(\w+)'", MIGRATION.split("UPDATE deployments d")[1].split("CREATE UNIQUE")[0])) >= {DeploymentStatus.DELETED}


def test_the_route_requires_an_idempotency_key(monkeypatch, dev_headers):
    from app.core.authz import Role

    client = make_client(routes.router, raise_server_exceptions=False)
    patch_tenant_conn(monkeypatch, FakeConn(access(roles=[Role.DEPLOYER])), service)
    r = client.post(f"/v1/projects/{PROJECT}/deployments?training_run_id={RUN}")
    assert r.status_code == 422


def _plan(limit):
    from datetime import UTC, datetime

    from tests.core.test_plan_limits import row

    return row(deployments={"max_active_deployments": limit}, updated_at=datetime.now(UTC))


def test_a_failing_cleanup_of_the_stray_serve_app_does_not_hide_the_409(env, monkeypatch):
    env.shared = False
    env.conn.fail = ("INSERT INTO deployments",)

    async def remove(**kw):
        raise RuntimeError("ray head is down")

    monkeypatch.setattr(service.serve_client, "remove", remove)
    with pytest.raises(HTTPException) as e:
        create()
    assert e.value.code == ProblemCode.DEPLOYMENT_EXISTS


def test_a_shared_create_that_loses_to_the_index_has_no_serve_app_to_remove(env):
    env.conn.fail = ("INSERT INTO deployments",)
    with pytest.raises(HTTPException):
        create()
    assert env.removed == [] and env.deployed == []


def test_a_main_create_that_loses_leaves_the_shared_main_app_to_the_winner(env):
    env.shared = False
    env.conn.vals["SELECT settings"] = {SETTING_DEPLOY_STRATEGY: DeployStrategy.MAIN}
    env.conn.fail = ("INSERT INTO deployments",)
    with pytest.raises(HTTPException):
        create()
    assert env.removed == []
