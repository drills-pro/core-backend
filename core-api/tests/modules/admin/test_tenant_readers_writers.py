"""The admin tenants route's SQL lives in the tenants context: typed rows in, the stored shape out."""
import asyncio
import datetime
import uuid

import pytest

from app.core.authz import TenantStatus
from app.core.authz.deps import TenantProblem
from app.core.problem import Problem
from app.core.enums import PlanTier
from app.modules.user.tenants import (
    admin_detail,
    admin_list,
    admin_org_ids,
    admin_platform_stats,
    insert_admin_tenant,
    merge_settings,
    set_status,
)
from tests.support import FakeConn

TENANT = uuid.uuid4()
BASE = {"id": TENANT, "name": "Acme", "zitadel_org_id": "o1", "plan_tier": "free", "created_at": datetime.datetime(2026, 1, 1)}
COUNTS = {"project_count": 2, "member_count": 3}


def test_admin_list_rows_are_typed_and_filters_are_passed_through():
    conn = FakeConn(rowset=[{**BASE, **COUNTS}])
    [row] = asyncio.run(admin_list(conn, "ac", "pro", 10))
    assert (row.name, row.project_count, row.member_count) == ("Acme", 2, 3)
    assert conn.calls[0][1] == ("ac", "pro", 10, PlanTier.INTERNAL)


def test_org_ids_and_platform_stats():
    assert asyncio.run(admin_org_ids(FakeConn(rowset=[{"zitadel_org_id": "o1"}, {"zitadel_org_id": None}]))) == ["o1", None]
    assert asyncio.run(admin_platform_stats(FakeConn(row={"tenant_count": 1}))) == {"tenant_count": 1}


def test_admin_detail_validates_settings_or_returns_none():
    assert asyncio.run(admin_detail(FakeConn(row=None), TENANT)) is None
    full = {**BASE, **COUNTS, "dataset_count": 4, "model_count": 5, "settings": {}}
    row = asyncio.run(admin_detail(FakeConn(row=full), TENANT))
    assert (row.dataset_count, row.model_count) == (4, 5)
    assert row.model_dump()["settings"] == row.settings.model_dump()


def test_writers():
    created = asyncio.run(insert_admin_tenant(FakeConn(row=BASE), "o1", "Acme", "free"))
    assert created.model_dump() == BASE
    assert asyncio.run(merge_settings(FakeConn(row=None), TENANT, {})) is None
    assert asyncio.run(merge_settings(FakeConn(row={"settings": {"a": 1}}), TENANT, {"a": 1})) == {"a": 1}
    assert asyncio.run(set_status(FakeConn(row=None), TENANT, TenantStatus.SUSPENDED)) is None
    with pytest.raises(Problem) as kept:  # the guard is in the UPDATE; the 409 reports it
        asyncio.run(set_status(FakeConn(row={"previous": TenantStatus.PENDING_DELETE, "org_id": ""}), TENANT, TenantStatus.ACTIVE))
    assert kept.value.code == TenantProblem.PENDING_DELETE
    conn = FakeConn(row={"previous": TenantStatus.ACTIVE, "org_id": ""})
    changed = asyncio.run(set_status(conn, TENANT, TenantStatus.SUSPENDED))
    assert (changed.previous, changed.org_id) == (TenantStatus.ACTIVE, "")
    assert conn.calls[0][1] == (TENANT, TenantStatus.SUSPENDED, TenantStatus.PENDING_DELETE)  # the guard is in the UPDATE
