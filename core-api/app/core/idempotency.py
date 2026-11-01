"""What every create endpoint that takes an `Idempotency-Key` shares: the header, the insert that loses a race
gracefully, and the answer to a key reused with a different request. What differs per endpoint stays in its
service: which checks run first, which lock serialises the quota, which fields make two requests "the same"."""
from collections.abc import Callable
from http import HTTPStatus
from typing import Annotated, Any

from fastapi import Header

from app.core.problem import Problem

IDEMPOTENCY_HEADER = "Idempotency-Key"
IDEMPOTENCY_KEY_MAX_LENGTH = 255

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
