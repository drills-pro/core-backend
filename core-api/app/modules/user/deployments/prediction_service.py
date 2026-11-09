"""What a key may do with a prediction it made: read it, cancel it, follow its events (planning/24). A key sees only its own, in its own
project; a miss, another key's prediction and a malformed id are the same 404."""
import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator

from app.core.authz import KeyContext
from app.core.problem import not_found
from app.core.tenant_conn import TenantConn
from app.modules.user.deployments import prediction_store as store
from app.modules.user.deployments.constants import (
    PREDICTION_ID_PREFIX,
    SSE_HEARTBEAT_SECONDS,
    SSE_POLL_SECONDS,
    TERMINAL_STATUSES,
    PredictionMode,
    PredictionStatus,
)
from app.modules.user.deployments.schemas import PredictionEnvelope

_OWNED = "SELECT * FROM predictions WHERE id = $1 AND api_key_id = $2 AND project_id = $3"
_OWNED_LOCKED = _OWNED + " FOR UPDATE"
HEARTBEAT = ": heartbeat\n\n"


def parse_id(prediction_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(prediction_id.removeprefix(PREDICTION_ID_PREFIX))
    except ValueError:
        raise not_found() from None


async def owned(conn, ctx: KeyContext, prediction_id: str, *, lock: bool = False):
    """The row of `prediction_id` when this key made it in this project (locked for the transaction when `lock`); the one 404 otherwise."""
    row = await conn.fetchrow(_OWNED_LOCKED if lock else _OWNED, parse_id(prediction_id), ctx.key_id, ctx.project_id)
    if row is None:
        raise not_found()
    return row


async def get(ctx: KeyContext, prediction_id: str) -> PredictionEnvelope:
    async with TenantConn(ctx.tenant_id) as conn:
        return await store.public(await owned(conn, ctx, prediction_id))


async def cancel(ctx: KeyContext, prediction_id: str) -> PredictionEnvelope:
    """queued -> cancelled at once; in_progress -> the worker is told and ends it; a finished prediction is answered as it is (idempotent)."""
    async with TenantConn(ctx.tenant_id) as conn:
        row = await owned(conn, ctx, prediction_id, lock=True)
        if row["status"] == PredictionStatus.QUEUED:
            row = await store.cancel_queued(conn, row)
        elif row["status"] == PredictionStatus.IN_PROGRESS and row["mode"] == PredictionMode.BACKGROUND:
            row = await store.request_cancel(conn, row)
        return await store.public(row)


def frame(seq: int, status: str, data: dict) -> str:
    """One server-sent event: the id (its seq in prediction_events) for Last-Event-ID, the status as the event name, the envelope or status as data."""
    return f"id: {seq}\nevent: {status}\ndata: {json.dumps(data or {'status': status})}\n\n"


async def _event_data(ctx: KeyContext, prediction_id: uuid.UUID, row) -> dict:
    """The data of one event. A completed event never holds an output that lives in the bucket (it would defeat the offload), so it is read back here."""
    if row["status"] != PredictionStatus.COMPLETED:
        return row["data"]
    async with TenantConn(ctx.tenant_id) as conn:
        stored = await store.get(conn, prediction_id)
    if stored is None or stored["output_ref"] is None:
        return row["data"]
    return {**row["data"], "output": await store.payload(stored, "output")}


async def follow(ctx: KeyContext, prediction_id: uuid.UUID, after: int) -> AsyncIterator[str]:
    """The events after `after`, then new ones as they are written, until a terminal one has been sent. A prediction whose events the
    retention purge dropped ends the stream at once; a quiet stream sends a heartbeat comment every SSE_HEARTBEAT_SECONDS."""
    last_write = time.monotonic()
    while True:
        async with TenantConn(ctx.tenant_id) as conn:
            status = await store.status_of(conn, prediction_id)  # read first: a terminal status means its event is already there
            rows = await store.events_after(conn, prediction_id, after)
        for row in rows:
            after, last_write = row["seq"], time.monotonic()
            yield frame(row["seq"], row["status"], await _event_data(ctx, prediction_id, row))
            if row["status"] in TERMINAL_STATUSES:
                return
        if status in TERMINAL_STATUSES and not rows:
            return
        if time.monotonic() - last_write >= SSE_HEARTBEAT_SECONDS:
            last_write = time.monotonic()
            yield HEARTBEAT
        await asyncio.sleep(SSE_POLL_SECONDS)


async def events(ctx: KeyContext, prediction_id: str, last_event_id: str | None) -> AsyncIterator[str]:
    """The event stream of a prediction this key made; a miss is raised here, before any streaming starts. A Last-Event-ID that is not a number replays from the start."""
    async with TenantConn(ctx.tenant_id) as conn:
        row = await owned(conn, ctx, prediction_id)
    return follow(ctx, row["id"], int(last_event_id) if last_event_id is not None and last_event_id.isdecimal() else 0)
