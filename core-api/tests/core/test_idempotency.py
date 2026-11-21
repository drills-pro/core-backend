import asyncio

import pytest

from app.core.idempotency import LOCK_TENANT_SQL, Cap, check_cap, insert_capped, insert_once, replay
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


COUNT = "SELECT count(*)"
FULL_CODE = "too_many"


class World:
    """One table and one advisory lock per key, like Postgres: a transaction's lock is held until it ends."""

    def __init__(self):
        self.rows, self.locks, self.lock_keys = [], {}, []


class TxConn:
    def __init__(self, world):
        self.world, self.held, self.calls = world, [], []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        for lock in self.held:
            lock.release()

    async def fetchval(self, sql, *args):
        self.calls.append(sql)
        if sql == LOCK_TENANT_SQL:
            lock = self.world.locks.setdefault(args[0], asyncio.Lock())
            await lock.acquire()
            self.held.append(lock)
            self.world.lock_keys.append(args[0])
            return None
        assert sql == COUNT
        count = len(self.world.rows)
        await asyncio.sleep(0)  # the answer takes time to arrive: without the lock a concurrent create slips in here
        return count

    async def fetchrow(self, sql, *args):
        self.calls.append(sql)
        if sql == "INSERT":
            await asyncio.sleep(0)
            self.world.rows.append(args)
            return {"id": args[0]}
        return None


async def _limit_of_one(conn, tenant_id):
    return 1


CAP = Cap("things:", COUNT, _limit_of_one, FULL_CODE, "full")


def _create(world, n):
    async def one():
        async with TxConn(world) as conn:
            return await insert_capped(conn, CAP, "t1", (), "INSERT", (n,), "SELECT", (n,))

    return one()


def test_the_cap_holds_when_creates_race_for_the_last_slot():
    async def race():
        world = World()
        results = await asyncio.gather(*[_create(world, n) for n in range(5)], return_exceptions=True)
        return world, results

    world, results = asyncio.run(race())
    assert len(world.rows) == 1
    full = [r for r in results if isinstance(r, Problem)]
    assert len(full) == 4 and all((p.status_code, p.code) == (429, FULL_CODE) for p in full)
    assert set(world.lock_keys) == {"things:t1"}  # the prefix and the tenant name the lock


def test_the_lock_is_taken_before_the_count_and_the_insert_after():
    world = World()

    async def one():
        async with TxConn(world) as conn:
            await insert_capped(conn, CAP, "t1", (), "INSERT", (1,), "SELECT", (1,))
            return conn.calls

    assert asyncio.run(one()) == [LOCK_TENANT_SQL, COUNT, "INSERT"]


def test_a_full_tenant_inserts_nothing_and_an_unlocked_check_takes_no_lock():
    world = World()
    world.rows.append(("existing",))

    async def go():
        async with TxConn(world) as conn:
            with pytest.raises(Problem):
                await insert_capped(conn, CAP, "t1", (), "INSERT", (2,), "SELECT", (2,))
        async with TxConn(world) as conn:
            with pytest.raises(Problem):
                await check_cap(conn, CAP, "t1")
            return conn.calls

    assert asyncio.run(go()) == [COUNT] and world.rows == [("existing",)]
