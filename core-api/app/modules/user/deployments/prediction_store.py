"""The predictions table (planning/24): the Idempotency-Key insert, the fenced write-back that ends a prediction, the events written
with every status change, and the envelope of a row. SQL only; who calls what, and when, is model_predict's and the worker's."""
import uuid
from datetime import datetime, timezone
from typing import Any

from app.core.authz import KeyContext
from app.core.idempotency import insert_once
from app.core.settings import settings
from app.integrations import data_plane
from app.integrations.data_plane import PREDICTION_INPUT_KEY, PREDICTION_OUTPUT_KEY
from app.modules.user.deployments.constants import (
    PREDICTION_ID_PREFIX,
    PredictionMode,
    PredictionStatus,
)
from app.modules.user.deployments.schemas import PredictionEnvelope, PredictionError, PredictModelRef, PredictRequest, PredictUsage
from app.modules.user.usage import UsageResource, UsageUnit, record_usage

SECONDS_PER_HOUR = 3600
SECONDS_PER_DAY = 86400

_INSERT = (
    "INSERT INTO predictions (id, input_ref, tenant_id, project_id, api_key_id, deployment_id, model_id, model_revision, mode, status, input, metadata, "
    "idempotency_key, request_hash, attempt, max_attempts, lease_expires_at, started_at, expires_at, source) "
    "VALUES (coalesce($17::uuid, gen_random_uuid()), $18, $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, now() + make_interval(secs => $15::float8), "
    "CASE WHEN $13 > 0 THEN now() END, now() + make_interval(secs => $16::float8), $19) "
    "ON CONFLICT (project_id, idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING RETURNING *"
)
_BY_KEY = "SELECT * FROM predictions WHERE project_id = $1 AND idempotency_key = $2"
_BY_ID = "SELECT * FROM predictions WHERE id = $1"
# Seq is one past the last: the writers of one prediction are serialised by the fenced UPDATE that precedes the event.
_EVENT = (
    "INSERT INTO prediction_events (prediction_id, seq, tenant_id, status, data) "
    "SELECT $1::uuid, coalesce(max(seq), 0) + 1, $2::uuid, $3::text, $4::jsonb FROM prediction_events WHERE prediction_id = $1"
)
# Ends a prediction its writer still owns: the attempt it claimed and the status it left it in. Zero rows = someone else owns it now.
_FINISH = (
    "UPDATE predictions SET status = $3, output = $4, output_ref = $10, error = $5, latency_ms = $6, finished_at = now(), "
    "expires_at = now() + make_interval(secs => $7::float8), lease_owner = NULL, lease_expires_at = NULL, "
    "idempotency_key = CASE WHEN $8 THEN NULL ELSE idempotency_key END "
    "WHERE id = $1 AND attempt = $2 AND status = $9 RETURNING *"
)
# A sync prediction whose handler died: past its lease, nobody will finish it.
_ABANDON = (
    "UPDATE predictions SET status = $2, error = $3, finished_at = now(), expires_at = now() + make_interval(secs => $4::float8), "
    "lease_expires_at = NULL WHERE id = $1 AND mode = $5 AND status = $6 AND lease_expires_at < now() RETURNING *"
)


# The worker's claim: a lease on up to $4 queued rows that are due, taken once (MATERIALIZED) and skipping rows another replica holds.
_CLAIM = (
    "WITH picked AS MATERIALIZED (SELECT id FROM predictions WHERE tenant_id = $1 AND mode = $2 AND status = $3 AND next_attempt_at <= now() "
    "ORDER BY created_at LIMIT $4 FOR UPDATE SKIP LOCKED) "
    "UPDATE predictions p SET status = $5, attempt = attempt + 1, lease_owner = $6, lease_expires_at = now() + make_interval(secs => $7::float8), "
    "started_at = coalesce(started_at, now()) FROM picked WHERE p.id = picked.id RETURNING p.*"
)
_EXPIRED = (
    "SELECT * FROM predictions WHERE tenant_id = $1 AND mode = $2 AND status = $3 AND lease_expires_at < now() "
    "ORDER BY created_at FOR UPDATE SKIP LOCKED"
)
_REQUEUE = (
    "UPDATE predictions SET status = $3, lease_owner = NULL, lease_expires_at = NULL, next_attempt_at = now() + make_interval(secs => $4::float8) "
    "WHERE id = $1 AND attempt = $2 AND status = $5 RETURNING *"
)


# Cancel (the caller holds the row lock, see prediction_service.owned): a queued row ends at once; an in_progress one is flagged and its worker ends it.
_CANCEL_QUEUED = (
    "UPDATE predictions SET status = $2, finished_at = now(), expires_at = now() + make_interval(secs => $3::float8), "
    "lease_owner = NULL, lease_expires_at = NULL WHERE id = $1 AND status = $4 RETURNING *"
)
_REQUEST_CANCEL = "UPDATE predictions SET cancel_requested = true WHERE id = $1 AND mode = $2 AND status = $3 RETURNING *"
# What a worker asks before it writes back: did the key cancel the attempt it holds? (The locked read makes the cancel endpoint wait for the write-back.)
_FLAG = "SELECT cancel_requested FROM predictions WHERE id = $1 AND attempt = $2 AND status = $3"
_FLAG_LOCKED = _FLAG + " FOR UPDATE"
_EVENTS_AFTER = "SELECT seq, status, data FROM prediction_events WHERE prediction_id = $1 AND seq > $2 ORDER BY seq"
_STATUS = "SELECT status FROM predictions WHERE id = $1"


def retention_seconds(mode: str) -> float:
    if mode == PredictionMode.SYNC:
        return settings.predict.sync_retention_hours * SECONDS_PER_HOUR
    return settings.predict.background_retention_days * SECONDS_PER_DAY


async def add_event(conn, row, data: dict[str, Any] | None = None) -> None:
    await conn.execute(_EVENT, row["id"], row["tenant_id"], row["status"], data or {})


async def begin(
    conn, ctx: KeyContext, deployment_id: uuid.UUID, model_id: uuid.UUID, revision: str, request: PredictRequest, *,
    mode: PredictionMode, key: str | None, request_hash: str | None, prediction_id: uuid.UUID | None = None, stored_input: data_plane.PredictionPayload | None = None,
):
    """(row, created): the new prediction, or the one an earlier request with this key made. A sync row starts in_progress on its
    first attempt (its handler owns it for a lease); a background row starts queued for the worker.
    An input already offloaded to the bucket (`stored_input`, under `prediction_id`, see stash_input) is kept as its ref."""
    stored_input = stored_input or data_plane.PredictionPayload(inline=request.input)
    sync = mode == PredictionMode.SYNC
    args = (
        ctx.tenant_id, ctx.project_id, ctx.key_id, deployment_id, model_id, revision, mode,
        PredictionStatus.IN_PROGRESS if sync else PredictionStatus.QUEUED, stored_input.inline, request.metadata, key, request_hash,
        1 if sync else 0, settings.predict.max_attempts,
        settings.predict.lease_seconds if sync else None, retention_seconds(mode) if sync else None, prediction_id, stored_input.ref, ctx.source,
    )
    row, created = await insert_once(conn, _INSERT, args, _BY_KEY, (ctx.project_id, key))
    if created:
        await add_event(conn, row)
    return row, created


async def claim(conn, tenant_id: str, limit: int, owner: str) -> list:
    """Lease up to `limit` of the tenant's due background predictions to `owner` (attempt + 1, in_progress). Run in a TenantConn that
    commits at once: two replicas claiming together get disjoint rows."""
    rows = await conn.fetch(
        _CLAIM, tenant_id, PredictionMode.BACKGROUND, PredictionStatus.QUEUED, limit, PredictionStatus.IN_PROGRESS, owner, settings.predict.lease_seconds)
    for row in rows:
        await add_event(conn, row)
    return rows


async def expired(conn, tenant_id: str) -> list:
    """The tenant's in_progress background predictions whose lease ran out (their worker is gone), locked for the caller's transaction."""
    return await conn.fetch(_EXPIRED, tenant_id, PredictionMode.BACKGROUND, PredictionStatus.IN_PROGRESS)


async def requeue(conn, row, delay_seconds: float):
    """Back to queued for another attempt after `delay_seconds`, fenced on the attempt the caller holds; None when it no longer owns the row."""
    moved = await conn.fetchrow(_REQUEUE, row["id"], row["attempt"], PredictionStatus.QUEUED, delay_seconds, PredictionStatus.IN_PROGRESS)
    if moved is not None:
        await add_event(conn, moved)
    return moved


async def cancel_queued(conn, row):
    """A queued prediction becomes cancelled at once (and says so in its events)."""
    moved = await conn.fetchrow(_CANCEL_QUEUED, row["id"], PredictionStatus.CANCELLED, retention_seconds(row["mode"]), PredictionStatus.QUEUED)
    await add_event(conn, moved, envelope(moved).model_dump(mode="json"))
    return moved


async def request_cancel(conn, row):
    """Flag a running background prediction; its worker ends it as cancelled and drops any result."""
    return await conn.fetchrow(_REQUEST_CANCEL, row["id"], PredictionMode.BACKGROUND, PredictionStatus.IN_PROGRESS)


async def cancel_requested(conn, row, *, lock: bool = False) -> bool:
    """Whether the attempt `row` holds has been cancelled; False when the row is no longer that attempt's."""
    return bool(await conn.fetchval(_FLAG_LOCKED if lock else _FLAG, row["id"], row["attempt"], PredictionStatus.IN_PROGRESS))


async def events_after(conn, prediction_id: uuid.UUID, seq: int) -> list:
    return await conn.fetch(_EVENTS_AFTER, prediction_id, seq)


async def status_of(conn, prediction_id: uuid.UUID) -> str | None:
    return await conn.fetchval(_STATUS, prediction_id)


async def get(conn, prediction_id: uuid.UUID):
    return await conn.fetchrow(_BY_ID, prediction_id)


async def finish(
    conn, row, status: PredictionStatus, *, output: Any = None, output_ref: str | None = None, error: PredictionError | None = None, latency_ms: float | None = None,
    release_key: bool = False,
):
    """End the prediction as `status`, fenced on the attempt the caller holds; None when it no longer owns the row. A completed
    one is metered in the same transaction, so a prediction id is billed once and only when it completed. `release_key` frees the
    Idempotency-Key (a failure that is not stored for replay). `output_ref` is an output already offloaded to the bucket (see stash_output)."""
    moved = await conn.fetchrow(
        _FINISH, row["id"], row["attempt"], status, output, error.model_dump() if error else None, latency_ms,
        retention_seconds(row["mode"]), release_key, PredictionStatus.IN_PROGRESS, output_ref)
    if moved is None:
        return None
    await add_event(conn, moved, envelope(moved).model_dump(mode="json"))
    if moved["status"] == PredictionStatus.COMPLETED:
        await record_usage(
            conn, tenant_id=str(moved["tenant_id"]), project_id=moved["project_id"], resource_type=UsageResource.INFERENCE, quantity=1,
            unit=UsageUnit.REQUESTS, occurred_at=datetime.now(timezone.utc), source_ref=str(moved["id"]))
    return moved


async def abandon_if_expired(conn, row, error: PredictionError):
    """Fail a sync prediction whose lease ran out; the failed row (or None when it is still held or already ended)."""
    moved = await conn.fetchrow(
        _ABANDON, row["id"], PredictionStatus.FAILED, error.model_dump(), retention_seconds(row["mode"]), PredictionMode.SYNC,
        PredictionStatus.IN_PROGRESS)
    if moved is not None:
        await add_event(conn, moved, envelope(moved).model_dump(mode="json"))
    return moved


async def stash_input(ctx: KeyContext, value: Any) -> tuple[uuid.UUID, data_plane.PredictionPayload]:
    """The id of a prediction about to be inserted and where its input goes: inline, or the bucket when it is over predict.inline_max_bytes."""
    prediction_id = uuid.uuid4()
    return prediction_id, await data_plane.store_prediction_payload(ctx.tenant_id, ctx.project_id, prediction_id, PREDICTION_INPUT_KEY, value)


async def stash_output(row, value: Any) -> data_plane.PredictionPayload:
    """Where the output of the attempt `row` holds goes (the attempt is in the key, so a stale attempt never overwrites the current one's).
    Written before the fenced write-back, outside any transaction; if the fence then drops it the object is left for the retention purge."""
    return await data_plane.store_prediction_payload(str(row["tenant_id"]), row["project_id"], row["id"], PREDICTION_OUTPUT_KEY, value, attempt=row["attempt"])


async def payload(row, column: str) -> Any:
    """The `input` or `output` of the row, read from the bucket when the row holds a ref."""
    ref = row[f"{column}_ref"]
    if ref is None:
        return row[column]
    return await data_plane.load_prediction_payload(str(row["tenant_id"]), row["project_id"], row["id"], ref)


async def public(row) -> PredictionEnvelope:
    """The envelope of the row with its output resolved (an output in the bucket is read back)."""
    return envelope({**dict(row), "output": await payload(row, "output")})


def envelope(row) -> PredictionEnvelope:
    extra = {"attempt": row["attempt"]} if row["mode"] == PredictionMode.BACKGROUND else {}
    return PredictionEnvelope(
        id=f"{PREDICTION_ID_PREFIX}{row['id']}", status=row["status"], model=PredictModelRef(id=row["deployment_id"]),
        output=row["output"], usage=PredictUsage(latency_ms=row["latency_ms"], **extra), metadata=row["metadata"],
        error=PredictionError.model_validate(row["error"]) if row["error"] else None, expired=row["purged_at"] is not None,
        created_at=row["created_at"])
