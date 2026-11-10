"""A pool whose one connection records every call, for code that only needs to see the SQL it sends."""


class RecordingConn:
    def __init__(self, fetchval_result=None):
        self.calls = []  # (method, sql, args)
        self.fetchval_result = fetchval_result

    async def fetchval(self, sql, *args):
        self.calls.append(("fetchval", sql, args))
        return self.fetchval_result

    async def execute(self, sql, *args):
        self.calls.append(("execute", sql, args))

    def transaction(self):
        conn = self

        class Tx:
            async def __aenter__(self):
                conn.calls.append(("tx_enter", "", ()))

            async def __aexit__(self, *exc):
                conn.calls.append(("tx_exit", "", exc[:1]))
                return False

        return Tx()


class FakePool:
    def __init__(self, conn):
        self.conn = conn
        self.released = 0

    def acquire(self):
        pool = self

        class Ctx:
            async def __aenter__(self):
                return pool.conn

            async def __aexit__(self, *exc):
                pool.released += 1
                return False

        return Ctx()


def patch_pool(monkeypatch, module, conn):
    pool = FakePool(conn)

    async def get_pool():
        return pool

    monkeypatch.setattr(module, "get_pool", get_pool)
    return pool
