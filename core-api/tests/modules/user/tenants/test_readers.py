"""What tenants offers other contexts."""
import asyncio

from app.core.enums import PlanTier
from app.modules.user.tenants import get_plan_tier
from tests.support import TENANT, FakeConn
import uuid


def test_plan_tier_is_a_plan_tier_or_none():
    tenant = uuid.UUID(TENANT)
    assert asyncio.run(get_plan_tier(FakeConn(val="pro"), tenant)) is PlanTier.PRO
    assert asyncio.run(get_plan_tier(FakeConn(val=None), tenant)) is None


def test_ref_slug_and_org_ids():
    from app.modules.user.tenants import get_org_ids, get_ref, slug_taken

    tenant = uuid.UUID(TENANT)
    assert asyncio.run(get_ref(FakeConn(row=None), tenant)) is None
    ref = asyncio.run(get_ref(FakeConn(row={"id": tenant, "name": "Acme", "slug": "acme"}), tenant))
    assert (ref.id, ref.name, ref.slug) == (tenant, "Acme", "acme")
    assert asyncio.run(slug_taken(FakeConn(val=True), "acme")) is True
    assert asyncio.run(slug_taken(FakeConn(val=None), "acme")) is False
    assert asyncio.run(get_org_ids(FakeConn(rowset=[{"id": tenant, "zitadel_org_id": "o1"}]), [tenant])) == {tenant: "o1"}
