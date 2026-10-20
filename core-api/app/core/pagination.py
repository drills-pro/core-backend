"""Keyset (cursor) pagination over (created_at, id), per the API contract: `page_token`/`page_size` in,
`next_page_token` in the body, never offset.

    page: PageParams = Depends()
    cond, args = keyset_clause(page.page_token, SortDir.ASC, first_param=2)   # $2, $3 are the cursor
    rows = await conn.fetch(f"SELECT * FROM t WHERE project_id=$1 {cond} "
                            f"ORDER BY {order_by(SortDir.ASC)} LIMIT {page.page_size + 1}", pid, *args)
    return build_page(rows, page.page_size, Item)
"""
import base64
import binascii
import enum
import json
from datetime import datetime
from typing import Any, Iterable
from uuid import UUID

from fastapi import Query
from pydantic import BaseModel

from app.core.problem import Problem

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
INVALID_TOKEN = "invalid_page_token"


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


def encode_token(created_at: datetime, id: UUID) -> str:
    raw = json.dumps([created_at.isoformat(), str(id)]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_token(token: str) -> tuple[datetime, UUID]:
    try:
        ts, id = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
        return datetime.fromisoformat(ts), UUID(id)
    except (ValueError, TypeError, binascii.Error):  # JSONDecodeError and UnicodeDecodeError are ValueErrors
        raise Problem(400, INVALID_TOKEN, "invalid page_token") from None


def order_by(direction: SortDir) -> str:
    return f"created_at {direction.value}, id {direction.value}"


def keyset_clause(token: str | None, direction: SortDir, first_param: int) -> tuple[str, list[Any]]:
    """('AND (created_at, id) > ($n, $n+1)', [ts, id]) for a token, ('', []) for the first page."""
    if token is None:
        return "", []
    ts, id = decode_token(token)
    n = first_param
    return f"AND (created_at, id) {_CMP[direction]} (${n}, ${n + 1})", [ts, id]


def build_page(rows: Iterable[Any], page_size: int, model: type[BaseModel]) -> Page:
    """`rows` were fetched with LIMIT page_size + 1: the extra row only signals a next page."""
    rows = list(rows)
    more = len(rows) > page_size
    rows = rows[:page_size]
    token = encode_token(rows[-1]["created_at"], rows[-1]["id"]) if more else None
    return Page(items=[model.model_validate(dict(r)) for r in rows], next_page_token=token)
