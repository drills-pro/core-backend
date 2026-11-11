"""Edges of the api_keys, model_registry, usage, tenant_members and deployments services that their route tests miss."""
import asyncio
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import asyncpg
import pytest
from starlette.exceptions import HTTPException

from app.core.authz import TenantRole
from app.integrations.data_plane.mlflow import ModelStage
from app.modules.user.api_keys import routes as key_routes
from app.modules.user.api_keys import service as key_service
from app.modules.user.deployments import routes as deployment_routes
from app.modules.user.deployments import service as deployment_service
from app.modules.user.model_registry import service as registry
from app.modules.user.tenant_members import routes as member_routes
from app.modules.user.tenant_members import service as member_service
from app.modules.user.tenant_members.constants import ProblemCode
from app.modules.user.usage import service as usage
from tests.support import PROJECT, TENANT, FakeConn, access, make_client, patch_tenant_conn

NOW = datetime.now(timezone.utc)


def test_revoking_an_unknown_key_is_404_and_nothing_is_audited(monkeypatch, dev_headers):
    conn = FakeConn(access(roles=["deployer"]), val=None)
    patch_tenant_conn(monkeypatch, conn, key_service)
    r = make_client(key_routes.router).delete(f"/v1/projects/{PROJECT}/api-keys/{uuid.uuid4()}")
    assert r.status_code == 404
    assert not any("audit_log" in sql for sql, _ in conn.calls)


def test_promoting_a_version_deleted_after_mlflow_moved_is_404(monkeypatch):
    moved = []

    async def transition_stage(*a):
        moved.append(a)

    monkeypatch.setattr(registry.mlflow, "transition_stage", transition_stage)
    row = {"mlflow_model_name": "m", "mlflow_version": "1"}
    conn = FakeConn(rows={"FROM model_versions WHERE id": row, "UPDATE model_versions SET stage": None})
    patch_tenant_conn(monkeypatch, conn, registry)
    with pytest.raises(HTTPException) as e:
        asyncio.run(registry.promote(SimpleNamespace(tenant_id=TENANT), PROJECT, uuid.uuid4(), ModelStage.PRODUCTION))
    assert e.value.status_code == 404 and len(moved) == 1


@pytest.mark.parametrize("returned,written", [({"id": 1}, True), (None, False)])
def test_usage_is_appended_once_per_source_and_stamped_with_the_projects_team(returned, written):
    team = uuid.uuid4()
    conn = FakeConn(val=team, row=returned)
    out = asyncio.run(usage.record_usage(conn, tenant_id=TENANT, project_id=PROJECT, resource_type="training", quantity=3,
                                         unit="gpu_seconds", occurred_at=NOW, source_ref="run-1"))
    assert out is written
    sql, args = conn.calls[-1]
    assert "ON CONFLICT (resource_type, source_ref) DO NOTHING" in sql
    assert args[2] == team and args[7] == "run-1"


def _members(monkeypatch, role, conn_cls=FakeConn, **kw):
    conn = conn_cls(access(role), **kw)
    patch_tenant_conn(monkeypatch, conn, member_service)
    return make_client(member_routes.router), conn


def test_pending_invitations_are_listed_for_an_admin_and_the_path_tenant_must_match(monkeypatch, dev_headers):
    row = {"id": uuid.uuid4(), "email": "a@b.co", "role": "member", "expires_at": NOW, "created_at": NOW}
    c, conn = _members(monkeypatch, TenantRole.ADMIN, rowset=[row])
    r = c.get(f"/v1/tenants/{TENANT}/invitations")
    assert (r.status_code, [i["email"] for i in r.json()]) == (200, ["a@b.co"])
    assert "accepted_at IS NULL" in conn.calls[-1][0]  # only the open ones
    assert c.get(f"/v1/tenants/{uuid.uuid4()}/invitations").status_code == 404
    c, _ = _members(monkeypatch, TenantRole.MEMBER)
    assert c.get(f"/v1/tenants/{TENANT}/invitations").status_code == 403


def test_a_second_ownership_offer_racing_the_first_is_409(monkeypatch, dev_headers):
    class Racing(FakeConn):
        async def fetchrow(self, sql, *args):
            if "INSERT INTO ownership_offers" in sql:
                raise asyncpg.UniqueViolationError()
            return await super().fetchrow(sql, *args)

    c, _ = _members(monkeypatch, TenantRole.OWNER, Racing, vals={"SELECT role FROM tenant_members": TenantRole.MEMBER})
    r = c.post(f"/v1/tenants/{TENANT}/ownership-offer", json={"to_sub": "u2"})
    assert (r.status_code, r.json()["code"]) == (409, ProblemCode.OFFER_PENDING)


def test_deploy_route_passes_the_run_and_the_key_and_answers_202(monkeypatch, dev_headers):
    run, seen = uuid.uuid4(), {}

    async def create_deployment(ctx, project_id, training_run_id, key, acknowledge_risk):
        seen.update(run=training_run_id, key=key, ack=acknowledge_risk)
        return {"id": uuid.uuid4(), "status": "deploying", "route_prefix": "/p", "rate_limit_per_minute": 60, "plan_tier": None, "predict_key": "drl_x"}

    monkeypatch.setattr(deployment_service, "create_deployment", create_deployment)
    patch_tenant_conn(monkeypatch, FakeConn(access(roles=["deployer"])), deployment_service)
    r = make_client(deployment_routes.router).post(
        f"/v1/projects/{PROJECT}/deployments?training_run_id={run}", headers={"Idempotency-Key": "k1"})
    assert (r.status_code, r.json()["predict_key"], seen) == (202, "drl_x", {"run": run, "key": "k1", "ack": False})
    assert make_client(deployment_routes.router).post(f"/v1/projects/{PROJECT}/deployments?training_run_id={run}").status_code == 422
