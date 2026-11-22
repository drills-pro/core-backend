"""Keyset (cursor) pagination over (created_at, id), per the API contract: `page_token`/`page_size` in,
`next_page_token` in the body, never offset.

    page: PageParams = Depends()
    cond, args = keyset_clause(page.page_token, SortDir.ASC, first_param=2)   # $2, $3 are the cursor
    rows = await conn.fetch(f"SELECT * FROM t WHERE project_id=$1 {cond} "
                            f"ORDER BY {order_by(SortDir.ASC)} LIMIT {page.page_size + 1}", pid, *args)
    return build_page(rows, page.page_size, Item)

A list that sorts on another column passes it to order_by, keyset_clause and build_page (`column=`, and
`id_type=` for a non-uuid id). A list that shows a total (the admin ones) rides count(*) OVER () on each row,
computed over the filtered set before the cursor cuts the page: `total_of(rows)` reads it and
`set_total_header` sends it as X-Total-Count.
"""
import base64
import binascii
import enum
import json
from datetime import datetime
from typing import Any, Iterable
from uuid import UUID

from fastapi import Query, Response
from pydantic import BaseModel

from app.core.problem import Problem

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
INVALID_TOKEN = "invalid_page_token"
CREATED_AT = "created_at"  # the default sort column; every table paged here has it, plus a unique id
TOTAL_COLUMN = "total"  # count(*) OVER () on every row: the pager's, not part of a row
TOTAL_HEADER = "X-Total-Count"


class SortDir(enum.StrEnum):
    ASC = "ASC"
    DESC = "DESC"


_CMP = {SortDir.ASC: ">", SortDir.DESC: "<"}  # fixed SQL fragments; user input never reaches them


class Page[T](BaseModel):
    items: list[T]
    next_page_token: str | None = None


class PageParams:
    def __init__(
        self,
        page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
        page_token: str | None = None,
    ):
        self.page_size = page_size
        self.page_token = page_token


def encode_token(sort_value: datetime, id: UUID | int) -> str:
    raw = json.dumps([sort_value.isoformat(), str(id)]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_token(token: str, id_type: type[UUID] | type[int] = UUID) -> tuple[datetime, UUID | int]:
    try:
        ts, id = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
        return datetime.fromisoformat(ts), id_type(id)
    except (ValueError, TypeError, AttributeError, binascii.Error):  # JSONDecodeError and UnicodeDecodeError are ValueErrors
        raise Problem(400, INVALID_TOKEN, "invalid page_token") from None


def order_by(direction: SortDir, column: str = CREATED_AT) -> str:
    return f"{column} {direction.value}, id {direction.value}"


def keyset_clause(
    token: str | None, direction: SortDir, first_param: int, column: str = CREATED_AT, id_type: type[UUID] | type[int] = UUID
) -> tuple[str, list[Any]]:
    """('AND (created_at, id) > ($n, $n+1)', [ts, id]) for a token, ('', []) for the first page."""
    if token is None:
        return "", []
    ts, id = decode_token(token, id_type)
    n = first_param
    return f"AND ({column}, id) {_CMP[direction]} (${n}, ${n + 1})", [ts, id]


def cursor_args(token: str | None) -> list[Any]:
    """[ts, id] of a token, [None, None] for the first page: the two cursor parameters of a SQL function that
    does its own keyset cut (the admin_* functions)."""
    return [None, None] if token is None else list(decode_token(token))


def build_page(rows: Iterable[Any], page_size: int, model: type[BaseModel], column: str = CREATED_AT) -> Page:
    """`rows` were fetched with LIMIT page_size + 1: the extra row only signals a next page."""
    rows = list(rows)
    more = len(rows) > page_size
    rows = rows[:page_size]
    token = encode_token(rows[-1][column], rows[-1]["id"]) if more else None
    return Page(items=[model.model_validate(dict(r)) for r in rows], next_page_token=token)


def total_of(rows: Iterable[Any]) -> int:
    """The size of the filtered set, from the first row. An empty page has no row to carry it, so it reports 0."""
    for row in rows:
        return row[TOTAL_COLUMN]
    return 0


def set_total_header(response: Response, total: int) -> None:
    response.headers[TOTAL_HEADER] = str(total)
