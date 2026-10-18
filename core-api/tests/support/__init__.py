"""Shared test doubles: a fake TenantConn database, the project_access row, and a problem+json app."""
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import problem
from app.core.authz import DefaultAccess, TenantRole, TenantStatus, Visibility, deps

TENANT, PROJECT = str(uuid.uuid4()), uuid.uuid4()
HEADERS = {"X-Tenant-Id": TENANT, "X-User-Id": "u1"}


def access(role=TenantRole.MEMBER, visibility=Visibility.INTERNAL, default_access=DefaultAccess.READ, roles=(), status=TenantStatus.ACTIVE):
    """The row project_access returns; `roles=None` / `visibility=None` model a project the caller cannot see."""
    return dict(tenant_role=role, tenant_status=status, visibility=visibility, default_access=default_access, roles=None if roles is None else list(roles))


def _canned(table, sql, default):
    return next((v for key, v in table.items() if key in sql), default)


class FakeConn:
    """Stands in for an asyncpg connection.

    project_access answers `access`; any other fetchrow / fetchval answers the first `rows` / `vals` entry whose key
    is a substring of the SQL, else `row` / `val`; fetch answers `rowset`. Every query lands in `calls` as (sql, args); the args of
    each execute() also land in `executed` (pass a list to share it across connections).
    """

    def __init__(self, access=None, *, row=None, val=PROJECT, rows=None, vals=None, rowset=(), result=None, executed=None):
        self.access, self.row, self.val, self.result = access, row, val, result
        self.rows, self.vals, self.rowset = rows or {}, vals or {}, list(rowset)
        self.calls = []
        self.executed = [] if executed is None else executed

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        return self.access if "project_access" in sql else _canned(self.rows, sql, self.row)

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        return _canned(self.vals, sql, self.val)

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return self.rowset

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        self.executed.append(args)
        return self.result


class TenantCtx:
    """`async with TenantConn(tenant_id) as conn` (or pool.acquire()) yielding `conn`."""

    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


def patch_tenant_conn(monkeypatch, conn, *modules):
    """Make deps and each module's TenantConn hand out `conn`."""
    for mod in (deps, *modules):
        monkeypatch.setattr(mod, "TenantConn", lambda tenant_id: TenantCtx(conn))


def make_client(*routers, headers=HEADERS, **kw):
    """A TestClient over a fresh app with the problem+json handler and `routers` mounted."""
    app = FastAPI()
    problem.install(app)
    for router in routers:
        app.include_router(router)
    return TestClient(app, headers=headers, **kw)
