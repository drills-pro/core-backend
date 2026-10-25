"""Deployment service rules, called directly (no HTTP)."""
import uuid

import asyncio

import pytest
from fastapi import HTTPException

from types import SimpleNamespace
from app.modules.user.deployments import service
from app.modules.user.deployments.constants import PLAN_TIER_RATE_LIMITS, PlanTier
from tests.support import FakeConn, patch_tenant_conn

CTX = SimpleNamespace(tenant_id=str(uuid.uuid4()))


def test_rate_limit_follows_plan_tier_and_unknown_falls_back_to_free():
    assert service.rate_limit_for(PlanTier.PRO) == PLAN_TIER_RATE_LIMITS[PlanTier.PRO]
    assert service.rate_limit_for("pro") == PLAN_TIER_RATE_LIMITS[PlanTier.PRO]
    assert service.rate_limit_for(None) == PLAN_TIER_RATE_LIMITS[PlanTier.FREE]


def test_deploy_requires_a_succeeded_run_with_an_artifact(monkeypatch):
    run = {"status": "failed", "model_artifact": None}
    patch_tenant_conn(monkeypatch, FakeConn(row=run), service)
    with pytest.raises(HTTPException) as e:
        asyncio.run(service.create_deployment(CTX, uuid.uuid4(), uuid.uuid4()))
    assert e.value.status_code == 409


def test_test_deployment_releases_the_connection_before_calling_serve(monkeypatch):
    open_conns = 0

    class Ctx:
        async def __aenter__(self):
            nonlocal open_conns
            open_conns += 1
            return FakeConn(row={"status": "active", "route_prefix": "/p"})

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
