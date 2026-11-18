import asyncio

from app.core import job_packages
from app.core.settings import settings


class _Conn:
    def __init__(self):
        self.calls = []

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        return 0


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


def test_the_audit_purge_asks_for_the_configured_number_to_keep_per_name(monkeypatch):
    conn = _Conn()

    async def get_pool():
        return _Pool(conn)

    monkeypatch.setattr(job_packages, "get_pool", get_pool)
    asyncio.run(job_packages.purge_audit())
    (sql, args), = conn.calls
    assert "purge_job_package_audit" in sql and args == (settings.job_packages.audit_keep_per_name,)
