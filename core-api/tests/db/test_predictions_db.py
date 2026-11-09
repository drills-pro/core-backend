"""The predictions and prediction_events tables as core_app on a real Postgres: the SQL of prediction_store.py (Idempotency-Key scope and
race, the attempt-fenced write-back, events, usage in the same transaction), the CHECKs that mirror the enums, and RLS."""
import json
from datetime import datetime, timezone

import asyncpg
import pytest

from app.core.authz import KeyContext
from app.modules.user.deployments import prediction_store as store
from app.modules.user.deployments.constants import PredictionMode, PredictionStatus
from app.modules.user.deployments.schemas import PredictionError, PredictRequest
from app.modules.user.usage import UsageResource, UsageUnit, record_usage

REQUEST = PredictRequest(input={"x": 1}, metadata={"m": 1})
HASH = "h" * 64
USAGE = "SELECT count(*) FROM usage_events WHERE resource_type = $1 AND source_ref = $2"


async def refs(t, tenant):
    """The ids of a seeded prediction's references. Also gives the connection what app/core/db.py gives the pool's: jsonb as Python objects."""
    await t.conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
    r = await t.conn.fetchrow("SELECT project_id, api_key_id, deployment_id, model_id FROM predictions WHERE tenant_id = $1", tenant)
    return KeyContext(r["api_key_id"], str(tenant), r["project_id"], frozenset(), frozenset()), r["deployment_id"], r["model_id"]


async def begin(conn, refs_, mode, key, request_hash=HASH):
    ctx, deployment, model = refs_
    return await store.begin(conn, ctx, deployment, model, "1", REQUEST, mode=mode, key=key, request_hash=request_hash)


async def events(conn, prediction_id):
    return [(r["seq"], r["status"]) for r in await conn.fetch("SELECT seq, status FROM prediction_events WHERE prediction_id = $1 ORDER BY seq", prediction_id)]


def test_a_sync_row_starts_in_progress_with_a_lease_and_24_hours_and_a_background_row_starts_queued(two_tenants):
    t = two_tenants

    async def go():
        r = await refs(t, t.a)
        async with t.as_tenant(t.a) as conn:
            sync, _ = await begin(conn, r, PredictionMode.SYNC, "s")
            background, _ = await begin(conn, r, PredictionMode.BACKGROUND, "b")
            hours = await conn.fetchval("SELECT extract(epoch FROM expires_at - created_at) / 3600 FROM predictions WHERE id = $1", sync["id"])
            assert (sync["status"], sync["attempt"], sync["max_attempts"], round(hours)) == (PredictionStatus.IN_PROGRESS, 1, 3, 24)
            assert sync["lease_expires_at"] is not None and sync["input"] == {"x": 1} and sync["metadata"] == {"m": 1}
            assert (background["status"], background["attempt"], background["lease_expires_at"], background["expires_at"]) == (PredictionStatus.QUEUED, 0, None, None)
            assert await events(conn, sync["id"]) == [(1, PredictionStatus.IN_PROGRESS)]
            assert await events(conn, background["id"]) == [(1, PredictionStatus.QUEUED)]

    t.run(go())


def test_the_key_is_unique_per_project_and_the_loser_of_the_race_gets_the_winners_row(two_tenants):
    t = two_tenants

    async def go():
        r = await refs(t, t.a)
        project = await t.conn.fetchval("INSERT INTO projects (tenant_id, name) VALUES ($1, 'second') RETURNING id", t.a)
        key = await t.conn.fetchval(
            "INSERT INTO api_keys (tenant_id, project_id, name, prefix, key_hash, created_by) VALUES ($1, $2, 'k', 'p', gen_random_uuid()::text, 'u1') RETURNING id",
            t.a, project)
        other = (KeyContext(key, str(t.a), project, frozenset(), frozenset()), r[1], r[2])
        async with t.as_tenant(t.a) as conn:
            won, created = await begin(conn, r, PredictionMode.SYNC, "k1")
            lost, created_again = await begin(conn, r, PredictionMode.BACKGROUND, "k1")
            elsewhere, created_elsewhere = await begin(conn, other, PredictionMode.SYNC, "k1")
            assert (created, created_again, created_elsewhere) == (True, False, True)
            assert lost["id"] == won["id"] and elsewhere["id"] != won["id"]
            assert await events(conn, won["id"]) == [(1, PredictionStatus.IN_PROGRESS)]  # the loser wrote none

    t.run(go())


def test_a_stale_attempt_cannot_end_a_prediction_and_the_owner_ends_it_once_with_one_usage_event(two_tenants):
    t = two_tenants

    async def go():
        r = await refs(t, t.a)
        async with t.as_tenant(t.a) as conn:
            row, _ = await begin(conn, r, PredictionMode.SYNC, "k")
            stale = {**row, "attempt": row["attempt"] + 1}
            assert await store.finish(conn, stale, PredictionStatus.COMPLETED, output={"o": 1}, latency_ms=1.0) is None
            assert (await store.get(conn, row["id"]))["status"] == PredictionStatus.IN_PROGRESS
            done = await store.finish(conn, row, PredictionStatus.COMPLETED, output={"o": 1}, latency_ms=2.5)
            assert (done["status"], done["output"], done["latency_ms"]) == (PredictionStatus.COMPLETED, {"o": 1}, 2.5)
            assert await store.finish(conn, row, PredictionStatus.COMPLETED, output={"o": 2}, latency_ms=9.0) is None  # no longer in_progress
            assert (await store.get(conn, row["id"]))["output"] == {"o": 1}
            assert await events(conn, row["id"]) == [(1, PredictionStatus.IN_PROGRESS), (2, PredictionStatus.COMPLETED)]
            assert await conn.fetchval(USAGE, UsageResource.INFERENCE, str(row["id"])) == 1

    t.run(go())


def test_a_failed_prediction_is_not_metered_and_release_key_frees_the_key(two_tenants):
    t = two_tenants

    async def go():
        r = await refs(t, t.a)
        async with t.as_tenant(t.a) as conn:
            kept, _ = await begin(conn, r, PredictionMode.SYNC, "kept")
            await store.finish(conn, kept, PredictionStatus.FAILED, error=PredictionError(code="upstream_error", detail="d"))
            freed, _ = await begin(conn, r, PredictionMode.SYNC, "freed")
            await store.finish(conn, freed, PredictionStatus.FAILED, error=PredictionError(code="no_servable_model", detail="d"), release_key=True)
            assert (await begin(conn, r, PredictionMode.SYNC, "kept"))[1] is False  # replayable
            again, created = await begin(conn, r, PredictionMode.SYNC, "freed")
            assert created and again["id"] != freed["id"]
            assert await conn.fetchval("SELECT count(*) FROM usage_events WHERE resource_type = $1 AND source_ref = ANY($2)",
                                       UsageResource.INFERENCE, [str(kept["id"]), str(freed["id"])]) == 0
            assert (await store.get(conn, kept["id"]))["error"] == {"code": "upstream_error", "detail": "d"}

    t.run(go())


def test_only_a_sync_row_past_its_lease_is_abandoned(two_tenants):
    t = two_tenants

    async def go():
        r = await refs(t, t.a)
        error = PredictionError(code="upstream_error", detail="gone")
        async with t.as_tenant(t.a) as conn:
            row, _ = await begin(conn, r, PredictionMode.SYNC, "k")
            assert await store.abandon_if_expired(conn, row, error) is None  # still held
            await conn.execute("UPDATE predictions SET lease_expires_at = now() - interval '1 second' WHERE id = $1", row["id"])
            moved = await store.abandon_if_expired(conn, row, error)
            assert moved["status"] == PredictionStatus.FAILED and moved["error"]["code"] == "upstream_error"
            assert await store.abandon_if_expired(conn, row, error) is None  # already ended
            background, _ = await begin(conn, r, PredictionMode.BACKGROUND, "b")
            await conn.execute("UPDATE predictions SET status = $2, lease_expires_at = now() - interval '1 second' WHERE id = $1", background["id"], PredictionStatus.IN_PROGRESS)
            assert await store.abandon_if_expired(conn, background, error) is None  # the worker's rows are reaped by the worker

    t.run(go())


def test_a_repeat_usage_write_for_one_prediction_id_is_absorbed(two_tenants):
    t = two_tenants

    async def go():
        r = await refs(t, t.a)
        async with t.as_tenant(t.a) as conn:
            row, _ = await begin(conn, r, PredictionMode.SYNC, "k")
            await store.finish(conn, row, PredictionStatus.COMPLETED, output={}, latency_ms=1.0)
            again = await record_usage(
                conn, tenant_id=str(t.a), project_id=row["project_id"], resource_type=UsageResource.INFERENCE, quantity=1,
                unit=UsageUnit.REQUESTS, occurred_at=datetime.now(timezone.utc), source_ref=str(row["id"]))
            assert again is False and await conn.fetchval(USAGE, UsageResource.INFERENCE, str(row["id"])) == 1

    t.run(go())


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE predictions SET status = 'done' WHERE tenant_id = $1",
        "UPDATE predictions SET mode = 'stream' WHERE tenant_id = $1",
        "UPDATE prediction_events SET status = 'done' WHERE tenant_id = $1",
    ],
)
def test_the_checks_refuse_a_value_outside_the_enums(two_tenants, sql):
    t = two_tenants

    async def go():
        with pytest.raises(asyncpg.CheckViolationError):
            await t.conn.execute(sql, t.a)

    t.run(go())


def test_tenant_a_cannot_see_tenant_bs_predictions_or_events(two_tenants):
    t = two_tenants

    async def go():
        async with t.as_tenant(t.a) as conn:
            tenants = {r[0] for r in await conn.fetch("SELECT tenant_id FROM predictions UNION SELECT tenant_id FROM prediction_events")}
            assert tenants == {t.a}

    t.run(go())
