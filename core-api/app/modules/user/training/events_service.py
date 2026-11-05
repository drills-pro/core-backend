"""Progress events of a training run: the token that lets the trainer post them, storing them, reading them back.

The trainer authenticates with a per-run random token. Only its sha256 is stored (training_runs.events_token_hash); the
token itself goes to the job through an environment variable and is in no row, spec or response.
"""
import hashlib
import hmac
import secrets
import uuid
from http import HTTPStatus
from typing import NamedTuple

from app.core.db import get_pool
from app.core.problem import Problem, ProblemCode as CoreProblemCode
from app.core.settings import settings
from app.core.tenant_conn import TenantConn
from app.integrations.job_spec import Reporting
from app.modules.user.training.constants import (
    EVENTS_PATH,
    EVENTS_TOKEN_BYTES,
    EVENTS_TOKEN_ENV,
    EventType,
    ProblemCode,
    TrainingStatus,
)
from app.modules.user.training.schemas import EventAck, ProgressEvent, ProgressPoint, RunProgress

_AUTH = "SELECT * FROM training_run_events_auth($1)"
# FOR SHARE: a settle that would end the run waits for this transaction, so no event lands after the run ended.
_STATUS = "SELECT status FROM training_runs WHERE id = $1 FOR SHARE"
_INSERT = (
    "INSERT INTO training_run_events (run_id, seq, tenant_id, type, epoch, epochs, metrics) "
    "VALUES ($1, $2, $3, $4, $5, $6, $7) ON CONFLICT (run_id, seq) DO NOTHING RETURNING seq"
)
_POINTS = "SELECT seq, epoch, epochs, metrics, created_at FROM training_run_events WHERE run_id = $1 ORDER BY seq"


class RunToken(NamedTuple):
    """What a new run gets: the secret for the job, and its hash for the row. `reporting` is the spec's part."""

    secret: str
    hash: str
    reporting: Reporting


def hash_token(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def mint(run_id: uuid.UUID) -> RunToken | None:
    """A token and the spec's `reporting` for a new run; None when no events URL is configured (progress stays off)."""
    base = settings.training.events_base_url.rstrip("/")
    if not base:
        return None
    secret = secrets.token_urlsafe(EVENTS_TOKEN_BYTES)
    return RunToken(secret, hash_token(secret), Reporting(events_url=base + EVENTS_PATH.format(run_id=run_id), token_env=EVENTS_TOKEN_ENV))


def _invalid_token() -> Problem:
    return Problem(HTTPStatus.UNAUTHORIZED, CoreProblemCode.INVALID_TOKEN, "missing or invalid events token")


async def authenticate(run_id: uuid.UUID, authorization: str | None) -> str:
    """The tenant of `run_id` when `authorization` carries its token. A missing run, a run without a channel and a wrong
    token are the same 401."""
    secret = (authorization or "").removeprefix("Bearer ")
    async with (await get_pool()).acquire() as conn:
        row = await conn.fetchrow(_AUTH, run_id)
    if row is None or not secret or not hmac.compare_digest(hash_token(secret), row["token_hash"]):
        raise _invalid_token()
    return str(row["tenant_id"])


async def record(tenant_id: str, run_id: uuid.UUID, event: ProgressEvent) -> EventAck:
    """Store the event; a `seq` the run already has changes nothing. Only a running run takes events."""
    async with TenantConn(tenant_id) as conn:
        status = await conn.fetchval(_STATUS, run_id)
        if status != TrainingStatus.RUNNING:
            raise Problem(HTTPStatus.CONFLICT, ProblemCode.INVALID_STATE, "run is not running")
        stored = await conn.fetchval(
            _INSERT, run_id, event.seq, tenant_id, EventType.PROGRESS, event.epoch, event.epochs, event.metrics)
    return EventAck(seq=event.seq, duplicate=stored is None)


async def progress_of(conn, run_id: uuid.UUID) -> RunProgress | None:
    """The run's reported epochs on the caller's connection (so under its RLS); None before the first one."""
    points = [ProgressPoint.model_validate(dict(r)) for r in await conn.fetch(_POINTS, run_id)]
    return RunProgress(latest=points[-1], history=points) if points else None
