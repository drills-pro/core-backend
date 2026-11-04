"""An internal (staff test) tenant gets its creator as owner; a customer tenant gets no member from the admin route."""
import asyncio
import datetime
import uuid
from types import SimpleNamespace

import pytest

from app.core.authz import TenantRole
from app.core.enums import PlanTier
from app.modules.admin.tenants import service
from app.modules.user.tenants.schemas import AdminTenantCreated
from tests.support import TenantCtx

TENANT = uuid.uuid4()
ACTOR = "staff-sub"


@pytest.fixture
def added(monkeypatch):
    calls = []

    async def get_pool():
        return SimpleNamespace(acquire=lambda: TenantCtx(None))

    async def insert(conn, org_id, name, plan_tier):
        return AdminTenantCreated(id=TENANT, name=name, zitadel_org_id=org_id, plan_tier=plan_tier,
                                  created_at=datetime.datetime(2026, 1, 1))

    async def noop(*args):
        pass

    async def add_member(conn, tenant_id, sub, role):
        calls.append((tenant_id, sub, role))

    monkeypatch.setattr(service, "get_pool", get_pool)
    monkeypatch.setattr(service.tenants_ctx, "insert_admin_tenant", insert)
    monkeypatch.setattr(service, "TenantConn", lambda tenant_id: TenantCtx(None))
    monkeypatch.setattr(service.teams, "create_team", noop)
    monkeypatch.setattr(service, "add_member", add_member)
    return calls


def test_internal_tenant_makes_the_creator_owner(added):
    asyncio.run(service._insert_tenant("o1", "QA", PlanTier.INTERNAL, ACTOR))
    assert added == [(TENANT, ACTOR, TenantRole.OWNER)]


def test_customer_tenant_gets_no_member(added):
    asyncio.run(service._insert_tenant("o1", "Acme", PlanTier.FREE, ACTOR))
    assert added == []
