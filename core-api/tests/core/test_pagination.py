from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import BaseModel

from app.core.pagination import SortDir, build_page, decode_token, encode_token, keyset_clause
from app.core.problem import Problem


class Item(BaseModel):
    id: object
    created_at: datetime


def _rows(n):
    return [{"id": uuid4(), "created_at": datetime(2026, 1, 1, 0, 0, i, tzinfo=UTC)} for i in range(n)]


def test_token_round_trip():
    ts, id = datetime.now(UTC), uuid4()
    assert decode_token(encode_token(ts, id)) == (ts, id)


@pytest.mark.parametrize("bad", ["!!!", "e30", "", "WyJ4IiwgInkiXQ"])
def test_garbage_token_is_400(bad):
    with pytest.raises(Problem) as e:
        decode_token(bad)
    assert (e.value.status_code, e.value.code) == (400, "invalid_page_token")


def test_page_size_plus_one_is_trimmed_and_token_points_at_last_kept_row():
    rows = _rows(3)
    page = build_page(rows, 2, Item)
    assert [i.id for i in page.items] == [r["id"] for r in rows[:2]]
    assert decode_token(page.next_page_token) == (rows[1]["created_at"], rows[1]["id"])


def test_last_page_has_no_token():
    assert build_page(_rows(2), 2, Item).next_page_token is None
    assert build_page([], 2, Item).next_page_token is None


def test_keyset_clause_direction_and_params():
    assert keyset_clause(None, SortDir.ASC, 2) == ("", [])
    ts, id = datetime.now(UTC), uuid4()
    tok = encode_token(ts, id)
    assert keyset_clause(tok, SortDir.ASC, 2) == ("AND (created_at, id) > ($2, $3)", [ts, id])
    assert keyset_clause(tok, SortDir.DESC, 4)[0] == "AND (created_at, id) < ($4, $5)"

