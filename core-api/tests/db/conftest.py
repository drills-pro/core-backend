"""Fixtures for the tests that need a real Postgres.

scripts/db-tests.sh starts one, applies the migrations and sets ISOLATION_DB_DSN (the owner, backend_admin). Without it
there is nothing to run: `pytest tests` skips this directory, and `pytest tests/db` alone finds no tests and exits 5,
so a CI step that lost the variable fails instead of passing on nothing.
"""
import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import asyncpg
import pytest

from app.core import tenant_conn
from tests.db.schema import APP_ROLE, SEEDS, TENANT_SEED
from tests.support import TenantCtx

DSN_ENV = "ISOLATION_DB_DSN"
collect_ignore_glob = [] if os.environ.get(DSN_ENV) else ["test_*.py"]


class TwoTenants:
    """Tenants `a` and `b`, each with a row in every tenant table, on one connection (`conn`, the owner) inside a
    transaction that is rolled back at the end, so nothing is left in the database. `run` drives async code from a test."""

    def __init__(self, loop, conn):
        self.loop, self.conn = loop, conn
        self.a, self.b = uuid.uuid4(), uuid.uuid4()

    def run(self, coro):
        return self.loop.run_until_complete(coro)

    @asynccontextmanager
    async def app_role(self):
        """core_app with no tenant set; whatever is done inside is undone on exit."""
        scope = self.conn.transaction()  # nested, so a savepoint
        await scope.start()
        try:
            await self.conn.execute(f"SET LOCAL ROLE {APP_ROLE}")
            yield self.conn
        finally:
            await scope.rollback()

    @asynccontextmanager
    async def as_tenant(self, tenant):
        """core_app scoped to `tenant` the way a request is, through TenantConn; undone on exit."""
        async with self.app_role():
            async with tenant_conn.TenantConn(str(tenant)) as conn:
                yield conn


@pytest.fixture
def two_tenants(monkeypatch):
    loop = asyncio.new_event_loop()
    conn = loop.run_until_complete(asyncpg.connect(os.environ[DSN_ENV]))
    transaction = conn.transaction()
    loop.run_until_complete(transaction.start())
    tenants = TwoTenants(loop, conn)

    async def seed():
        # RLS has to hold for every command whatever core_app is granted today, so give it all of them (rolled back too).
        await conn.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {', '.join(table for table, _ in SEEDS)} TO {APP_ROLE}")
        for tenant in (tenants.a, tenants.b):
            await conn.execute(TENANT_SEED, tenant)
            for _, sql in SEEDS:
                await conn.execute(sql, tenant)

    async def one_connection_pool():
        return SimpleNamespace(acquire=lambda: TenantCtx(conn))

    monkeypatch.setattr(tenant_conn, "get_pool", one_connection_pool)  # TenantConn runs on this connection, inside the transaction
    try:
        tenants.run(seed())
        yield tenants
    finally:
        loop.run_until_complete(transaction.rollback())
        loop.run_until_complete(conn.close())
        loop.close()
