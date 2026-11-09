"""Cancel and the event stream as core_app on a real Postgres: the ownership scope of a key, cancel of each state, the attempt-fenced cancel
flag a worker reads, and the events a stream replays."""
import pytest

from app.core.authz import KeyContext
from app.core.problem import Problem
from app.modules.user.deployments import prediction_service as service, prediction_store as store
from app.modules.user.deployments.constants import PREDICTION_ID_PREFIX, PredictionMode, PredictionStatus
from tests.db.test_predictions_db import begin, events, quiet, refs


def pid(row):
    return f"{PREDICTION_ID_PREFIX}{row['id']}"


def test_a_key_sees_only_its_own_predictions_in_its_own_project(two_tenants):
    t = two_tenants

    async def go():
        ctx, deployment, model = await refs(t, t.a)
        other_key = await t.conn.fetchval(
            "INSERT INTO api_keys (tenant_id, project_id, name, prefix, key_hash, created_by) VALUES ($1, $2, 'k2', 'p', gen_random_uuid()::text, 'u1') RETURNING id",
            t.a, ctx.project_id)
        async with t.as_tenant(t.a) as conn:
            row, _ = await begin(conn, (ctx, deployment, model), PredictionMode.BACKGROUND, "k")
            assert (await service.owned(conn, ctx, pid(row)))["id"] == row["id"]
            stranger = KeyContext(other_key, ctx.tenant_id, ctx.project_id, ctx.scopes, ctx.model_ids)  # same project, another key
            with pytest.raises(Problem) as miss:
                await service.owned(conn, stranger, pid(row))
            assert miss.value.status_code == 404
            for bad in ("nonsense", f"{PREDICTION_ID_PREFIX}{row['id']}x", "pred_"):
                with pytest.raises(Problem):
                    await service.owned(conn, ctx, bad)

    t.run(go())


def test_tenant_bs_key_cannot_reach_tenant_as_prediction(two_tenants):
    t = two_tenants

    async def go():
        a_ctx, deployment, model = await refs(t, t.a)
        b_ctx, _, _ = await refs(t, t.b)
        async with t.as_tenant(t.a) as conn:
            row, _ = await begin(conn, (a_ctx, deployment, model), PredictionMode.BACKGROUND, "k")
        async with t.as_tenant(t.b) as conn:
            with pytest.raises(Problem) as miss:
                await service.owned(conn, b_ctx, pid(row))
            assert miss.value.status_code == 404

    t.run(go())


def test_cancel_ends_a_queued_prediction_flags_a_running_one_and_leaves_a_finished_one_alone(two_tenants):
    t = two_tenants

    async def go():
        ctx, deployment, model = await refs(t, t.a)
        r = (ctx, deployment, model)
        await quiet(t, t.a)
        async with t.as_tenant(t.a) as conn:
            queued, _ = await begin(conn, r, PredictionMode.BACKGROUND, "q")
            cancelled = await service.cancel(ctx, pid(queued))
            assert cancelled.status == PredictionStatus.CANCELLED
            row = await store.get(conn, queued["id"])
            assert row["finished_at"] is not None and row["expires_at"] is not None and row["cancel_requested"] is False
            assert await events(conn, queued["id"]) == [(1, PredictionStatus.QUEUED), (2, PredictionStatus.CANCELLED)]
            assert (await service.cancel(ctx, pid(queued))).status == PredictionStatus.CANCELLED  # idempotent
            assert len(await events(conn, queued["id"])) == 2

            await begin(conn, r, PredictionMode.BACKGROUND, "run")
            (running,) = await store.claim(conn, str(t.a), 1, "w")
            assert (await service.cancel(ctx, pid(running))).status == PredictionStatus.IN_PROGRESS
            flagged = await store.get(conn, running["id"])
            assert (flagged["status"], flagged["cancel_requested"]) == (PredictionStatus.IN_PROGRESS, True)

            done, _ = await begin(conn, r, PredictionMode.SYNC, "s")
            await store.finish(conn, done, PredictionStatus.COMPLETED, output={}, latency_ms=1.0)
            assert (await service.cancel(ctx, pid(done))).status == PredictionStatus.COMPLETED

            sync, _ = await begin(conn, r, PredictionMode.SYNC, "s2")  # a sync call is its handler's: no flag
            assert (await service.cancel(ctx, pid(sync))).status == PredictionStatus.IN_PROGRESS
            assert (await store.get(conn, sync["id"]))["cancel_requested"] is False

    t.run(go())


def test_the_cancel_flag_is_read_for_the_attempt_the_worker_holds(two_tenants):
    t = two_tenants

    async def go():
        ctx, deployment, model = await refs(t, t.a)
        await quiet(t, t.a)
        async with t.as_tenant(t.a) as conn:
            await begin(conn, (ctx, deployment, model), PredictionMode.BACKGROUND, "b")
            (row,) = await store.claim(conn, str(t.a), 1, "w")
            assert await store.cancel_requested(conn, row) is False
            await service.cancel(ctx, pid(row))
            assert await store.cancel_requested(conn, row) is True and await store.cancel_requested(conn, row, lock=True) is True
            assert await store.cancel_requested(conn, {**row, "attempt": row["attempt"] + 1}) is False  # a stale attempt reads nothing
            moved = await store.finish(conn, row, PredictionStatus.CANCELLED)
            assert moved["status"] == PredictionStatus.CANCELLED and moved["output"] is None
            assert await store.cancel_requested(conn, row) is False  # no longer in_progress
            assert await conn.fetchval("SELECT count(*) FROM usage_events WHERE source_ref = $1", str(row["id"])) == 0  # a cancelled one is not metered

    t.run(go())


def test_the_event_stream_replays_after_a_seq_and_ends_after_the_terminal_event(two_tenants):
    t = two_tenants

    async def go():
        ctx, deployment, model = await refs(t, t.a)
        await quiet(t, t.a)
        async with t.as_tenant(t.a) as conn:
            await begin(conn, (ctx, deployment, model), PredictionMode.BACKGROUND, "b")
            (row,) = await store.claim(conn, str(t.a), 1, "w")
            await store.finish(conn, row, PredictionStatus.COMPLETED, output={"o": 1}, latency_ms=1.0)
            for after, expected in ((0, [1, 2, 3]), (1, [2, 3]), (3, [])):
                frames = [f async for f in await service.events(ctx, pid(row), str(after))]
                assert [line.split("\n")[0] for line in frames] == [f"id: {i}" for i in expected]
            last = [f async for f in await service.events(ctx, pid(row), None)][-1]
            assert last.startswith("id: 3\nevent: completed\n") and '"output": {"o": 1}' in last

    t.run(go())
