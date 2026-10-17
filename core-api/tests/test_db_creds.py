"""The pool follows the `db` secret file: unchanged creds keep the pool, new creds swap it and close the old one."""
import asyncio
import json

from app.core import db, secret_files
from app.core.settings import settings


class FakePool:
    def __init__(self, dsn):
        self.dsn, self.closed = dsn, False

    async def close(self):
        self.closed = True


async def _fake_create_pool(dsn, **_):
    return FakePool(dsn)


def test_pool_follows_creds_file(monkeypatch, tmp_path):
    path = tmp_path / "db.json"
    path.write_text(json.dumps({"username": "u1", "password": "p1"}))
    monkeypatch.setattr(settings.secrets, "db", path)
    secret_files._cache.clear()
    monkeypatch.setattr(db.asyncpg, "create_pool", _fake_create_pool)
    monkeypatch.setattr(db, "WATCH_SECONDS", 0.01)
    monkeypatch.setattr(db, "_pool", None)
    monkeypatch.setattr(db, "_pool_creds", None)

    async def run():
        first = await db.get_pool()
        try:
            assert "u1:p1@" in first.dsn
            await asyncio.sleep(0.05)  # several watch ticks, file unchanged
            assert db._pool is first and not first.closed

            path.write_text(json.dumps({"username": "u2", "password": "p2"}))
            await asyncio.sleep(0.05)
            assert "u2:p2@" in db._pool.dsn and first.closed
        finally:
            db._watch_task.cancel()

    asyncio.run(run())
