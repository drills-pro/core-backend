"""What every create endpoint that takes an `Idempotency-Key` shares: the header, the insert that loses a race
gracefully, the answer to a key reused with a different request, and for a create counted against a plan limit the
tenant lock, the cap check under it and the insert. What differs per endpoint stays in its service: which checks run first,
which statuses count, which fields make two requests "the same"."""
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from http import HTTPStatus
from typing import Annotated, Any

from fastapi import Header

from app.core.problem import Problem

IDEMPOTENCY_HEADER = "Idempotency-Key"
IDEMPOTENCY_KEY_MAX_LENGTH = 255

LOCK_TENANT_SQL = "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))"  # held until the transaction ends

IdempotencyKey = Annotated[str, Header(alias=IDEMPOTENCY_HEADER, min_length=1, max_length=IDEMPOTENCY_KEY_MAX_LENGTH)]


async def insert_once(conn, insert_sql: str, insert_args: tuple, by_key_sql: str, key_args: tuple) -> tuple[Any, bool]:
    """Run an `INSERT ... ON CONFLICT (..., idempotency_key) DO NOTHING RETURNING *`. Returns (row, created): the new
    row, or when a request with the same key won the race, that one's row."""
    row = await conn.fetchrow(insert_sql, *insert_args)
    if row is not None:
        return row, True
    return await conn.fetchrow(by_key_sql, *key_args), False


def replay(row, same_request: Callable[[Any], bool], mismatch_code: str) -> Any:
    """The row an earlier request with this key created; 409 `mismatch_code` when this request is a different one."""
    if not same_request(row):
        raise Problem(HTTPStatus.CONFLICT, mismatch_code, "this Idempotency-Key was used with a different request")
    return row


@dataclass(frozen=True)
class Cap:
    """A plan limit on how many of a tenant's rows may be live at once: the advisory-lock key prefix that serialises its
    writes, the count query ($1 is the tenant, `count_args` the rest), the plan's limit, and the 429 answer when it is full."""

    lock_prefix: str
    count_sql: str
    limit: Callable[[Any, str], Awaitable[int]]  # (conn, tenant_id) -> the plan's limit
    full_code: str
    full_detail: str


async def check_cap(conn, cap: Cap, tenant_id: str, *count_args, lock: bool = False) -> None:
    """429 when the tenant already has as many as the plan allows. `lock` takes the tenant's advisory lock first, so that
    two concurrent creates cannot both take the last slot: without it the answer is only an early, cheap one."""
    if lock:
        await conn.fetchval(LOCK_TENANT_SQL, f"{cap.lock_prefix}{tenant_id}")  # a concurrent create waits here, then sees this one
    if await conn.fetchval(cap.count_sql, tenant_id, *count_args) >= await cap.limit(conn, tenant_id):
        raise Problem(HTTPStatus.TOO_MANY_REQUESTS, cap.full_code, cap.full_detail)


async def insert_capped(
    conn, cap: Cap, tenant_id: str, count_args: tuple, insert_sql: str, insert_args: tuple, by_key_sql: str, key_args: tuple
) -> tuple[Any, bool]:
    """Lock the tenant, check the cap, then `insert_once`. Returns (row, created) like insert_once."""
    await check_cap(conn, cap, tenant_id, *count_args, lock=True)
    return await insert_once(conn, insert_sql, insert_args, by_key_sql, key_args)
