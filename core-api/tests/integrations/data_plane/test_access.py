"""Ownership checks: the owning contexts register their query, the gateway runs it as the tenant."""
import asyncio
import uuid

import pytest

from app.core.problem import Problem
from app.integrations.data_plane import access
from tests.support import FakeConn, TenantCtx

T, A, B = (str(uuid.uuid4()) for _ in range(3))


@pytest.fixture
def checkers(monkeypatch):
    monkeypatch.setattr(access, "_checkers", {})
    monkeypatch.setattr(access, "TenantConn", lambda tenant_id: TenantCtx(FakeConn()))
    return access._checkers


def test_every_resource_has_an_owner_once_the_contexts_are_imported():
    assert set(access._checkers) == set(access.Resource)


def test_a_resource_nobody_registered_is_refused(checkers):
    with pytest.raises(RuntimeError, match="no ownership check"):
        asyncio.run(access.project(T, A))


def test_the_checker_gets_the_connection_the_tenant_and_the_ids_in_gateway_order(checkers):
    seen = {}

    async def checker(conn, tenant_id, *ids):
        seen.update(conn=conn, tenant=tenant_id, ids=ids)
        return "commit"

    access.register(access.Resource.VERSION, checker)
    assert asyncio.run(access.version_commit(T, A, B)) == "commit"  # (tenant, dataset, version) in
    assert (seen["tenant"], seen["ids"]) == (uuid.UUID(T), (uuid.UUID(B), uuid.UUID(A)))  # (version, dataset) out
    assert seen["conn"] is not None


def test_a_checker_that_finds_nothing_is_a_404(checkers):
    async def checker(conn, tenant_id, *ids):
        return None

    access.register(access.Resource.PROJECT, checker)
    with pytest.raises(Problem) as e:
        asyncio.run(access.project(T, A))
    assert e.value.status_code == 404


def test_a_malformed_id_is_a_404_before_any_checker_runs(checkers):
    with pytest.raises(Problem) as e:
        asyncio.run(access.project(T, "not-a-uuid"))
    assert e.value.status_code == 404
