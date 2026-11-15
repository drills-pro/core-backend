"""The admin status writer as core_app: it moves an active tenant and leaves a pending_delete one alone (audit A-04)."""
import uuid

from app.core.authz import TenantStatus
from app.modules.user.tenants import set_status

_STATUS = "SELECT status FROM tenants WHERE id = $1"


def test_set_status_never_moves_a_pending_delete_tenant(two_tenants):
    t = two_tenants

    async def go():
        await t.conn.execute("UPDATE tenants SET status = $2 WHERE id = $1", t.a, TenantStatus.PENDING_DELETE)
        async with t.app_role() as conn:
            kept = await set_status(conn, t.a, TenantStatus.ACTIVE)
            moved = await set_status(conn, t.b, TenantStatus.SUSPENDED)
            assert (kept.previous, await conn.fetchval(_STATUS, t.a)) == (TenantStatus.PENDING_DELETE, TenantStatus.PENDING_DELETE)
            assert (moved.previous, await conn.fetchval(_STATUS, t.b)) == (TenantStatus.ACTIVE, TenantStatus.SUSPENDED)
            assert await set_status(conn, uuid.uuid4(), TenantStatus.ACTIVE) is None  # no such tenant

    t.run(go())
