from app.db import get_pool


class TenantConn:
    """Acquires a pooled connection and scopes it to one tenant via RLS
    (`SET LOCAL app.tenant_id`) for the lifetime of one request's transaction.
    """

    def __init__(self, tenant_id: str):
        self.tenant_id = tenant_id

    async def __aenter__(self):
        pool = await get_pool()
        self._conn_ctx = pool.acquire()
        self.conn = await self._conn_ctx.__aenter__()
        self._tx = self.conn.transaction()
        await self._tx.__aenter__()
        await self.conn.execute("SELECT set_config('app.tenant_id', $1, true)", self.tenant_id)
        return self.conn

    async def __aexit__(self, *exc):
        await self._tx.__aexit__(*exc)
        await self._conn_ctx.__aexit__(*exc)
