import asyncio

from app.core import rate_limit


def test_check_allows_up_to_limit_then_denies(monkeypatch):
    counts = {}

    class Conn:
        async def fetchval(self, sql, key, window):
            counts[(key, window)] = counts.get((key, window), 0) + 1
            return counts[(key, window)]

    class Pool:
        def acquire(self):
            class Ctx:
                async def __aenter__(s):
                    return Conn()

                async def __aexit__(s, *e):
                    return False

            return Ctx()

    async def get_pool():
        return Pool()

    monkeypatch.setattr(rate_limit, "get_pool", get_pool)
    monkeypatch.setattr(rate_limit.time, "time", lambda: 600.0)
    results = [asyncio.run(rate_limit.check("d", 3)) for _ in range(5)]
    assert results == [True, True, True, False, False]
