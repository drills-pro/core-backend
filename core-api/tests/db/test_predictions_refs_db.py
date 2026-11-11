"""Payload refs of the predictions table as core_app on a real Postgres: the inline-or-ref CHECKs, the ref written by begin and finish, and the
purge that touches a row holding an object only once its object is reported deleted."""
import uuid

import asyncpg
import pytest

from app.modules.user.deployments import prediction_store as store
from app.modules.user.deployments.constants import PredictionMode, PredictionStatus
from app.integrations.data_plane.prediction_payloads import Payload
from tests.db.test_predictions_db import REQUEST, begin, quiet, refs

INPUT_REF = "predictions/t/p/i/input.json"
OUTPUT_REF = "predictions/t/p/i/1/output.json"


def test_a_row_cannot_hold_both_an_inline_payload_and_its_ref(two_tenants):
    t = two_tenants

    async def go():
        r = await refs(t, t.a)
        async with t.as_tenant(t.a) as conn:
            row, _ = await begin(conn, r, PredictionMode.BACKGROUND, "b")
            for column in ("input", "output"):
                with pytest.raises(asyncpg.CheckViolationError, match=f"predictions_{column}_inline_or_ref_check"):
                    async with conn.transaction():
                        await conn.execute(f"UPDATE predictions SET {column} = '1', {column}_ref = 'x' WHERE id = $1", row["id"])

    t.run(go())


def test_begin_keeps_an_offloaded_input_as_its_ref_under_the_given_id_and_finish_keeps_the_output_ref(two_tenants):
    t = two_tenants

    async def go():
        ctx, deployment, model = await refs(t, t.a)
        prediction_id = uuid.uuid4()
        async with t.as_tenant(t.a) as conn:
            row, created = await store.begin(
                conn, ctx, deployment, model, "1", REQUEST, mode=PredictionMode.SYNC, key="k", request_hash="h" * 64,
                prediction_id=prediction_id, stored_input=Payload(ref=INPUT_REF))
            assert created and (row["id"], row["input"], row["input_ref"]) == (prediction_id, None, INPUT_REF)
            done = await store.finish(conn, row, PredictionStatus.COMPLETED, output_ref=OUTPUT_REF, latency_ms=1.0)
            assert (done["output"], done["output_ref"]) == (None, OUTPUT_REF)

    t.run(go())


def test_the_purge_leaves_a_row_with_an_object_until_it_is_reported_cleaned_then_clears_the_refs(two_tenants):
    t = two_tenants

    async def go():
        r = await refs(t, t.a)
        await quiet(t, t.a)
        async with t.as_tenant(t.a) as conn:
            made = {}
            for name, mode in (("sync", PredictionMode.SYNC), ("background", PredictionMode.BACKGROUND), ("inline", PredictionMode.BACKGROUND)):
                row, _ = await begin(conn, r, mode, name)
                made[name] = row["id"]
                await conn.execute(
                    "UPDATE predictions SET input = $2::jsonb, input_ref = $3, status = $4, expires_at = now() - interval '1 minute' WHERE id = $1",
                    row["id"], '{"x": 1}' if name == "inline" else None, None if name == "inline" else INPUT_REF, PredictionStatus.COMPLETED)
            listed = {row["id"] for row in await conn.fetch("SELECT * FROM expiring_prediction_refs()")}
            assert listed == {made["sync"], made["background"]}  # the inline row holds no object
            assert await conn.fetchval("SELECT purge_predictions()") == 1  # only the inline row: the others wait for their objects' deletion
            assert (await store.get(conn, made["sync"])) is not None and (await store.get(conn, made["background"]))["purged_at"] is None
            assert await conn.fetchval("SELECT purge_predictions($1::uuid[])", [made["sync"]]) == 1
            assert await store.get(conn, made["sync"]) is None  # deleted with its events
            assert await conn.fetchval("SELECT purge_predictions($1::uuid[])", [made["background"]]) == 1
            scrubbed = await store.get(conn, made["background"])
            assert (scrubbed["input"], scrubbed["input_ref"], scrubbed["output_ref"], scrubbed["purged_at"] is not None) == (None, None, None, True)
            assert await conn.fetchval("SELECT count(*) FROM expiring_prediction_refs()") == 0

    t.run(go())
