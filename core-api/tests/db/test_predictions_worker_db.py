"""The prediction worker's SQL as core_app on a real Postgres: the claim, the lost-lease query, the fenced requeue, the cross-tenant tenant
list and the retention purge."""
from app.modules.user.deployments import prediction_store as store
from app.modules.user.deployments.constants import PredictionMode, PredictionStatus
from tests.db.test_predictions_db import begin, events, quiet, refs


def test_the_claim_leases_only_due_queued_background_rows_up_to_the_limit(two_tenants):
    t = two_tenants

    async def go():
        r = await refs(t, t.a)
        await quiet(t, t.a)
        async with t.as_tenant(t.a) as conn:
            rows = [(await begin(conn, r, PredictionMode.BACKGROUND, f"b{i}"))[0] for i in range(3)]
            sync, _ = await begin(conn, r, PredictionMode.SYNC, "s")
            await conn.execute("UPDATE predictions SET next_attempt_at = now() + interval '1 hour' WHERE id = $1", rows[2]["id"])
            claimed = await store.claim(conn, str(t.a), 5, "me")
            assert {c["id"] for c in claimed} == {rows[0]["id"], rows[1]["id"]}
            for c in claimed:
                assert (c["status"], c["attempt"], c["lease_owner"], c["started_at"] is not None) == (PredictionStatus.IN_PROGRESS, 1, "me", True)
                assert c["lease_expires_at"] is not None
                assert await events(conn, c["id"]) == [(1, PredictionStatus.QUEUED), (2, PredictionStatus.IN_PROGRESS)]
            assert (await store.get(conn, rows[2]["id"]))["status"] == PredictionStatus.QUEUED  # waiting for its backoff
            assert (await store.get(conn, sync["id"]))["attempt"] == 1  # a sync row is its handler's
            assert await store.claim(conn, str(t.a), 5, "other") == []  # nothing is claimed twice

    t.run(go())


def test_expired_finds_background_rows_past_their_lease_and_requeue_is_fenced_on_the_attempt(two_tenants):
    t = two_tenants

    async def go():
        r = await refs(t, t.a)
        await quiet(t, t.a)
        async with t.as_tenant(t.a) as conn:
            await begin(conn, r, PredictionMode.BACKGROUND, "b")
            (row,) = await store.claim(conn, str(t.a), 1, "me")
            assert await store.expired(conn, str(t.a)) == []  # the lease is live
            await conn.execute("UPDATE predictions SET lease_expires_at = now() - interval '1 second' WHERE id = $1", row["id"])
            (lost,) = await store.expired(conn, str(t.a))
            assert lost["id"] == row["id"]
            assert await store.requeue(conn, {**lost, "attempt": 9}, 5) is None  # a stale attempt moves nothing
            moved = await store.requeue(conn, lost, 30)
            assert (moved["status"], moved["lease_owner"], moved["lease_expires_at"], moved["attempt"]) == (PredictionStatus.QUEUED, None, None, 1)
            wait = await conn.fetchval("SELECT extract(epoch FROM next_attempt_at - now()) FROM predictions WHERE id = $1", row["id"])
            assert 25 < wait <= 30
            assert await store.requeue(conn, lost, 30) is None  # no longer in_progress
            assert await events(conn, row["id"]) == [(1, PredictionStatus.QUEUED), (2, PredictionStatus.IN_PROGRESS), (3, PredictionStatus.QUEUED)]

    t.run(go())


def test_inflight_prediction_tenant_ids_lists_every_tenant_with_background_work(two_tenants):
    t = two_tenants

    async def go():
        await refs(t, t.a)
        async with t.as_tenant(t.a) as conn:
            assert {r[0] for r in await conn.fetch("SELECT inflight_prediction_tenant_ids()")} >= {t.a, t.b}  # across tenants, by design
        await quiet(t, t.a)
        async with t.as_tenant(t.a) as conn:
            ids = {r[0] for r in await conn.fetch("SELECT inflight_prediction_tenant_ids()")}
            assert t.a not in ids and t.b in ids

    t.run(go())


def test_the_purge_deletes_expired_sync_rows_and_scrubs_expired_background_ones(two_tenants):
    t = two_tenants

    async def go():
        r = await refs(t, t.a)
        await quiet(t, t.a)
        async with t.as_tenant(t.a) as conn:
            made = {}
            for name, mode in (("sync_old", PredictionMode.SYNC), ("sync_new", PredictionMode.SYNC), ("bg_old", PredictionMode.BACKGROUND), ("bg_new", PredictionMode.BACKGROUND)):
                row, _ = await begin(conn, r, mode, name)
                await conn.execute("UPDATE predictions SET output = '{\"o\": 1}', status = $2 WHERE id = $1", row["id"], PredictionStatus.COMPLETED)
                made[name] = row["id"]
            for name in ("sync_old", "bg_old"):
                await conn.execute("UPDATE predictions SET expires_at = now() - interval '1 minute' WHERE id = $1", made[name])
            for name in ("sync_new", "bg_new"):
                await conn.execute("UPDATE predictions SET expires_at = now() + interval '1 hour' WHERE id = $1", made[name])
            assert await conn.fetchval("SELECT purge_predictions()") >= 2
            assert await store.get(conn, made["sync_old"]) is None and await events(conn, made["sync_old"]) == []  # the row and its events are gone
            scrubbed = await store.get(conn, made["bg_old"])
            assert (scrubbed["input"], scrubbed["output"], scrubbed["status"], scrubbed["purged_at"] is not None) == (None, None, PredictionStatus.COMPLETED, True)
            assert await events(conn, made["bg_old"]) == [] and store.envelope(scrubbed).expired is True
            for kept in ("sync_new", "bg_new"):
                row = await store.get(conn, made[kept])
                assert row["output"] == {"o": 1} and row["purged_at"] is None and await events(conn, made[kept])
            assert await conn.fetchval("SELECT purge_predictions()") == 0  # nothing left to do

    t.run(go())
