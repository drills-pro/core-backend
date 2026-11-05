"""The classes backfill, with a fake database and a fake model.json store."""
import asyncio
import json
import uuid
from types import SimpleNamespace

from app.modules.user.training import backfill_classes as bf
from tests.support import FakeConn, TenantCtx

TENANT, PROJECT = uuid.uuid4(), uuid.uuid4()
RUNS = sorted(uuid.uuid4() for _ in range(5))


class World:
    """The candidate query (keyset by id, like the SQL function), model.json by run id, and what was written."""

    def __init__(self, monkeypatch, metas):
        self.metas, self.written, self.reads = metas, [], []
        conn = FakeConn(executed=self.written)

        async def fetch(sql, task, after, limit):
            rows = [r for r in RUNS if after is None or r > after][:limit]
            return [{"tenant_id": TENANT, "project_id": PROJECT, "id": r} for r in rows]

        async def acquire_pool():
            return SimpleNamespace(acquire=lambda: TenantCtx(SimpleNamespace(fetch=fetch)))

        async def read_model_meta(tenant_id, project_id, run_id):
            self.reads.append(run_id)
            meta = self.metas[uuid.UUID(run_id)]
            if isinstance(meta, Exception):
                raise meta
            return meta

        monkeypatch.setattr(bf, "get_pool", acquire_pool)
        monkeypatch.setattr(bf.data_plane, "read_model_meta", read_model_meta)
        monkeypatch.setattr(bf, "TenantConn", lambda tenant_id: TenantCtx(conn))


def go(**kw):
    return asyncio.run(bf.run(**kw))


def test_classes_from_model_json_are_merged_into_each_report(monkeypatch):
    w = World(monkeypatch, {r: {"classes": [f"c{i}"]} for i, r in enumerate(RUNS)})
    assert go(batch_size=2) == bf.Summary(updated=5)  # three batches
    assert [(a[0], json.loads(a[1])) for a in w.written] == [(r, [f"c{i}"]) for i, r in enumerate(RUNS)]


def test_a_dry_run_reads_and_writes_nothing(monkeypatch):
    w = World(monkeypatch, {r: {"classes": ["a"]} for r in RUNS})
    assert go(dry_run=True) == bf.Summary(updated=5)
    assert len(w.reads) == 5 and w.written == []


def test_one_bad_run_does_not_stop_the_others(monkeypatch):
    metas = {r: {"classes": ["a"]} for r in RUNS}
    metas[RUNS[0]] = RuntimeError("storage down")
    metas[RUNS[1]] = None  # no model.json
    metas[RUNS[2]] = {"classes": []}
    w = World(monkeypatch, metas)
    assert go(batch_size=2) == bf.Summary(updated=2, skipped=2, failed=1)
    assert [a[0] for a in w.written] == RUNS[3:]


def test_max_runs_limits_how_many_are_looked_at(monkeypatch):
    w = World(monkeypatch, {r: {"classes": ["a"]} for r in RUNS})
    assert go(batch_size=2, max_runs=3) == bf.Summary(updated=3)
    assert len(w.reads) == 3
