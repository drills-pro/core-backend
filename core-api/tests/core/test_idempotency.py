import asyncio

import pytest

from app.core.idempotency import insert_once, replay
from app.core.problem import Problem

MISMATCH = "idempotency_mismatch"


class Conn:
    """fetchrow answers the insert first (None: it lost the race), then the by-key lookup."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        return self.answers.pop(0)


def test_a_new_key_returns_the_inserted_row():
    conn = Conn({"id": 1})
    assert asyncio.run(insert_once(conn, "INSERT", (1, "k"), "SELECT", ("k",))) == ({"id": 1}, True)
    assert [c[0] for c in conn.calls] == ["INSERT"]


def test_losing_the_race_returns_the_winners_row():
    conn = Conn(None, {"id": 2})
    assert asyncio.run(insert_once(conn, "INSERT", (1, "k"), "SELECT", ("k",))) == ({"id": 2}, False)
    assert conn.calls[1] == ("SELECT", ("k",))


def test_replay_returns_the_row_for_the_same_request_and_409s_for_another():
    row = {"id": 3}
    assert replay(row, lambda r: True, MISMATCH) is row
    with pytest.raises(Problem) as e:
        replay(row, lambda r: False, MISMATCH)
    assert e.value.status_code == 409 and e.value.code == MISMATCH
