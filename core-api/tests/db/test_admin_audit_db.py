"""admin_audit on a real Postgres as core_app: written in the caller's transaction, readable, never changed or removed."""
import json

import asyncpg
import pytest

from app.core import admin_audit
from app.core.enums import AdminAuditAction, AdminAuditEntity

ACTOR = "ops-audit-test"


def _record(conn, entity_id="pro"):
    return admin_audit.record(conn, ACTOR, AdminAuditAction.PLAN_LIMITS_PUT, AdminAuditEntity.PLAN_LIMITS, entity_id, {"tier": entity_id})


def _with_codec(two_tenants):
    two_tenants.run(two_tenants.conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"))


def test_a_row_is_written_and_read_back(two_tenants):
    _with_codec(two_tenants)

    async def write():
        async with two_tenants.app_role() as conn:
            await _record(conn)
            return dict(await conn.fetchrow("SELECT * FROM admin_audit WHERE actor = $1", ACTOR))

    row = two_tenants.run(write())
    assert (row["action"], row["entity_type"], row["entity_id"], row["detail"]) == (
        AdminAuditAction.PLAN_LIMITS_PUT, AdminAuditEntity.PLAN_LIMITS, "pro", {"tier": "pro"})


def test_the_row_rolls_back_with_the_write_it_records(two_tenants):
    _with_codec(two_tenants)

    async def failing_write():
        async with two_tenants.app_role() as conn:
            with pytest.raises(RuntimeError):
                async with conn.transaction():
                    await _record(conn)
                    raise RuntimeError("the write after the audit row failed")
            return await conn.fetchval("SELECT count(*) FROM admin_audit WHERE actor = $1", ACTOR)

    assert two_tenants.run(failing_write()) == 0


@pytest.mark.parametrize("sql", ["UPDATE admin_audit SET actor = 'x'", "DELETE FROM admin_audit", "TRUNCATE admin_audit"])
def test_core_app_cannot_change_or_remove_a_row(two_tenants, sql):
    _with_codec(two_tenants)

    async def change():
        async with two_tenants.app_role() as conn:
            await _record(conn)
            await conn.execute(sql)

    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        two_tenants.run(change())


@pytest.mark.parametrize("action, entity", [("tenant.explode", AdminAuditEntity.TENANT), (AdminAuditAction.TENANT_CREATE, "dataset")])
def test_the_checks_hold(two_tenants, action, entity):
    _with_codec(two_tenants)

    async def bad():
        async with two_tenants.app_role() as conn:
            await admin_audit.record(conn, ACTOR, action, entity, "x")

    with pytest.raises(asyncpg.CheckViolationError):
        two_tenants.run(bad())
