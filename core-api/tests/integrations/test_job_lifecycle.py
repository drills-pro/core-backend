"""job_lifecycle: the deadline policy, the guarded transition, the claim and the sweep, against an in-memory table
that enforces the compare-and-set and the lease (tests/support/job_table.py)."""
import asyncio
import logging
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import SimpleNamespace

import pytest

from app.core.problem import Problem
from app.core.settings import load
from app.integrations import compute, job_lifecycle
from app.integrations.compute import JobStatus
from app.integrations.job_lifecycle import (
    CLAIM_BATCH_MAX,
    CLAIM_LEASE_SECONDS,
    SETTLE_BACKOFF_BASE_SECONDS,
    SETTLE_BACKOFF_MAX_SECONDS,
    SETTLE_ERROR_AFTER,
    SWEEP_CONCURRENCY,
    JobTable,
    Phase,
    Verdict,
)
from tests.support.job_table import FakeJobConn, FakeJobTable, patch

T0 = datetime(2026, 1, 1, tzinfo=UTC)


class S(StrEnum):
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


JOB = JobTable(
    name="thing", table="things", tenants_fn="inflight_thing_tenant_ids", inflight=(S.RUNNING,),
    transitions={S.RUNNING: frozenset({S.DONE, S.FAILED}), S.DONE: frozenset(), S.FAILED: frozenset()},
    columns=frozenset({"report"}), deadline=lambda row: row.get("deadline"),
)
NO_COMPLETED = JOB.model_copy(update={"completed_col": None})


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def table(monkeypatch):
    t = FakeJobTable(JOB)
    patch(monkeypatch, t)
    return t


@pytest.fixture
def ray(monkeypatch):
    answers = {}

    async def status(job_id):
        answer = answers.get(job_id, JobStatus.RUNNING)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(compute, "status", status)
    return answers


# ---- judge: the one deadline / no-answer policy ----

@pytest.mark.parametrize("ray_state,deadline,expected", [
    (JobStatus.RUNNING, T0 + timedelta(seconds=1), False),  # not due yet
    (JobStatus.RUNNING, T0, True),  # due exactly now
    (None, T0, True),  # Ray gave no answer: the deadline still ends it
    (None, T0 + timedelta(seconds=1), False),  # ...but nothing is decided before it
    (JobStatus.PENDING, T0, True),
    (JobStatus.SUCCEEDED, T0 - timedelta(days=1), False),  # a terminal answer wins even after the deadline
    (JobStatus.FAILED, T0 - timedelta(days=1), False),
    (JobStatus.STOPPED, T0 - timedelta(days=1), False),
    (JobStatus.RUNNING, None, False),  # no deadline at all
])
def test_judge_timed_out(ray_state, deadline, expected):
    verdict = job_lifecycle.judge(JOB, {"deadline": deadline}, ray_state, T0)
    assert verdict.timed_out is expected
    assert verdict.ray == ray_state and verdict.phase == (job_lifecycle.phase(ray_state) if ray_state else None)


@pytest.mark.parametrize("status", list(JobStatus))
def test_every_ray_state_has_a_phase(status):
    assert job_lifecycle.phase(status) in Phase


# ---- advance ----

def test_advance_moves_along_an_edge_sets_columns_and_stamps_completion(table):
    row = table.seed(S.RUNNING, claimed_until=T0, report=None)

    async def go():
        async with table(None) as conn:
            return await job_lifecycle.advance(conn, JOB, row, S.DONE, report={"ok": True})

    moved = run(go())
    assert (moved["status"], moved["report"], row["claimed_until"]) == (S.DONE, {"ok": True}, None)
    assert moved["completed_at"] is not None


def test_advance_with_no_completed_column_stamps_nothing(table):
    row = table.seed(S.RUNNING)

    async def go():
        async with table(None) as conn:
            return await job_lifecycle.advance(conn, NO_COMPLETED, row, S.DONE)

    assert run(go())["completed_at"] is None


def test_advance_refuses_what_is_not_an_edge_without_touching_the_row(table):
    row = table.seed(S.DONE)

    async def go():
        async with table(None) as conn:
            return await job_lifecycle.advance(conn, JOB, row, S.RUNNING)

    assert run(go()) is None and row["status"] == S.DONE


def test_advance_loses_quietly_when_someone_moved_the_row_first(table):
    stale = dict(table.seed(S.RUNNING))
    table.rows[stale["id"]]["status"] = S.FAILED  # another writer got there

    async def go():
        async with table(None) as conn:
            return await job_lifecycle.advance(conn, JOB, stale, S.DONE)

    assert run(go()) is None and table.rows[stale["id"]]["status"] == S.FAILED


def test_a_second_advance_of_the_same_row_moves_it_once(table):
    row = table.seed(S.RUNNING)
    snapshot = dict(row)

    async def go():
        async with table(None) as conn:
            return [await job_lifecycle.advance(conn, JOB, snapshot, S.DONE), await job_lifecycle.advance(conn, JOB, snapshot, S.DONE)]

    first, second = run(go())
    assert first is not None and second is None


def test_advance_rejects_a_column_the_table_did_not_declare(table):
    row = table.seed(S.RUNNING)

    async def go():
        async with table(None) as conn:
            await job_lifecycle.advance(conn, JOB, row, S.DONE, status="x")

    with pytest.raises(ValueError):
        run(go())


# ---- claim ----

def _claim(table, limit=CLAIM_BATCH_MAX):
    async def go():
        async with table(None) as conn:
            return await job_lifecycle.claim(conn, JOB, str(table.tenant_id), limit)

    return run(go())


def test_two_claims_get_disjoint_rows(table):
    ids = {table.seed(S.RUNNING)["id"] for _ in range(5)}
    first, second = _claim(table, 3), _claim(table, 3)
    got = [r["id"] for r in first] + [r["id"] for r in second]
    assert len(first) == 3 and len(second) == 2 and set(got) == ids and len(got) == len(set(got))


def test_claim_takes_the_oldest_first_and_only_in_flight_rows_of_the_tenant(table):
    old, new = table.seed(S.RUNNING), table.seed(S.RUNNING)
    table.seed(S.DONE)
    other = FakeJobTable(JOB)
    table.rows.update(other.rows)
    foreign = table.seed(S.RUNNING, tenant_id=other.tenant_id)
    assert [r["id"] for r in _claim(table, 1)] == [old["id"]]
    assert [r["id"] for r in _claim(table, 5)] == [new["id"]]
    assert foreign["claimed_until"] is None


def test_a_lease_that_ran_out_can_be_claimed_again(table):
    row = table.seed(S.RUNNING)
    assert len(_claim(table)) == 1 and _claim(table) == []
    table.now += timedelta(seconds=CLAIM_LEASE_SECONDS - 1)
    assert _claim(table) == []  # a crashed replica's rows wait out the lease...
    table.now += timedelta(seconds=2)
    assert [r["id"] for r in _claim(table)] == [row["id"]]  # ...and are then taken over


# ---- sweep ----

class Settled:
    """A settle that records what it saw, with a little concurrency of its own."""

    def __init__(self, table, move_to=None, fail_on=()):
        self.table, self.move_to, self.fail_on = table, move_to, fail_on
        self.seen, self.live, self.max_live, self.open_during = [], 0, 0, []

    async def __call__(self, row, verdict):
        self.live += 1
        self.max_live = max(self.max_live, self.live)
        self.open_during.append(self.table.open)
        try:
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            if row["id"] in self.fail_on:
                raise RuntimeError("boom")
            self.seen.append((row["id"], verdict))
            if self.move_to is not None:
                async with self.table(None) as conn:
                    await job_lifecycle.advance(conn, JOB, row, self.move_to)
        finally:
            self.live -= 1


def test_sweep_settles_every_in_flight_row_and_hands_settle_no_connection(table, ray):
    rows = [table.seed(S.RUNNING, ray_job_id=f"j{i}") for i in range(3)]
    ray["j1"] = JobStatus.SUCCEEDED
    settle = Settled(table, move_to=S.DONE)
    run(job_lifecycle.sweep(JOB, settle, T0))
    verdicts = dict(settle.seen)
    assert verdicts[rows[1]["id"]] == Verdict(ray=JobStatus.SUCCEEDED, phase=Phase.SUCCEEDED, timed_out=False)
    assert verdicts[rows[0]["id"]].phase is Phase.IN_FLIGHT
    assert [r["status"] for r in rows] == [S.DONE] * 3 and all(r["claimed_until"] is None for r in rows)
    assert settle.open_during == [0, 0, 0]  # nothing is held while settle talks to Ray, S3 or MLflow


def test_sweep_runs_at_most_sweep_concurrency_rows_at_once_and_still_finishes_them_all(table, ray):
    for _ in range(CLAIM_BATCH_MAX):
        table.seed(S.RUNNING)
    settle = Settled(table, move_to=S.DONE)
    run(job_lifecycle.sweep(JOB, settle, T0))
    assert settle.max_live == SWEEP_CONCURRENCY
    assert len(settle.seen) == CLAIM_BATCH_MAX and {r["status"] for r in table.rows.values()} == {S.DONE}


def test_sweep_takes_at_most_a_batch_per_tenant(table, ray):
    for _ in range(CLAIM_BATCH_MAX + 5):
        table.seed(S.RUNNING)
    settle = Settled(table)
    run(job_lifecycle.sweep(JOB, settle, T0))
    assert len(settle.seen) == CLAIM_BATCH_MAX  # the rest wait for the next tick


def test_an_unmoved_row_is_released_to_be_polled_on_the_next_tick(table, ray):
    row = table.seed(S.RUNNING)
    settle = Settled(table)  # moves nothing
    run(job_lifecycle.sweep(JOB, settle, T0))
    assert row["claimed_until"] is None and row["status"] == S.RUNNING
    run(job_lifecycle.sweep(JOB, settle, T0))
    assert len(settle.seen) == 2


def test_a_release_does_not_wipe_a_later_claimers_lease(table, ray):
    row = table.seed(S.RUNNING)

    async def slow_settle(claimed, verdict):
        table.now += timedelta(seconds=CLAIM_LEASE_SECONDS + 1)  # the lease lapses mid-settle...
        async with table(None) as conn:  # ...and another replica claims the row
            assert len(await job_lifecycle.claim(conn, JOB, str(table.tenant_id), 1)) == 1

    run(job_lifecycle.sweep(JOB, slow_settle, T0))
    assert row["claimed_until"] is not None and row["claimed_until"] > table.now  # the second claim survives the first release


def test_one_failing_row_neither_blocks_the_sweep_nor_is_retried_before_its_backoff(table, ray):
    bad, good = table.seed(S.RUNNING), table.seed(S.RUNNING)
    settle = Settled(table, move_to=S.DONE, fail_on={bad["id"]})
    run(job_lifecycle.sweep(JOB, settle, T0))
    assert (bad["status"], good["status"]) == (S.RUNNING, S.DONE)
    assert bad["settle_attempts"] == 1 and bad["claimed_until"] == table.now + timedelta(seconds=SETTLE_BACKOFF_BASE_SECONDS)
    run(job_lifecycle.sweep(JOB, settle, T0))
    assert bad["settle_attempts"] == 1  # still backing off: not claimed, not settled again


@pytest.mark.parametrize("attempts,seconds", [(1, 10), (2, 20), (3, 40), (5, 160), (6, 300), (50, 300)])
def test_settle_backoff_doubles_up_to_the_cap(attempts, seconds):
    assert job_lifecycle.settle_backoff(attempts) == seconds == min(seconds, SETTLE_BACKOFF_MAX_SECONDS)


def test_a_row_that_keeps_failing_backs_off_longer_each_time_and_a_cured_row_settles_after_its_backoff(table, ray):
    row = table.seed(S.RUNNING)
    settle = Settled(table, move_to=S.DONE, fail_on={row["id"]})
    waited = []
    for n in range(1, 8):
        run(job_lifecycle.sweep(JOB, settle, T0))
        assert row["settle_attempts"] == n
        waited.append((row["claimed_until"] - table.now).total_seconds())
        table.now = row["claimed_until"] + timedelta(seconds=1)
    assert waited == [10, 20, 40, 80, 160, 300, 300]
    settle.fail_on = ()
    run(job_lifecycle.sweep(JOB, settle, T0))
    assert row["status"] == S.DONE and row["settle_attempts"] == 0 and row["claimed_until"] is None
    assert [i for i, _ in settle.seen] == [row["id"]]  # settled once, by the cured attempt


def test_a_settle_that_works_without_moving_the_row_resets_its_failures(table, ray):
    row = table.seed(S.RUNNING, settle_attempts=3)
    run(job_lifecycle.sweep(JOB, Settled(table), T0))  # moves nothing, raises nothing
    assert row["settle_attempts"] == 0 and row["claimed_until"] is None


def test_a_move_resets_the_failures(table):
    row = table.seed(S.RUNNING, settle_attempts=4)

    async def go():
        async with table(None) as conn:
            await job_lifecycle.advance(conn, JOB, row, S.DONE)

    run(go())
    assert row["settle_attempts"] == 0


def test_the_failure_is_logged_at_error_once_after_the_threshold(table, ray, caplog):
    row = table.seed(S.RUNNING)
    settle = Settled(table, fail_on={row["id"]})
    with caplog.at_level(logging.WARNING, logger=job_lifecycle.logger.name):
        for _ in range(SETTLE_ERROR_AFTER + 3):
            run(job_lifecycle.sweep(JOB, settle, T0))
            table.now = row["claimed_until"] + timedelta(seconds=1)
    levels = [r.levelno for r in caplog.records if "failed to follow" in r.message]
    assert levels.count(logging.ERROR) == 1 and levels.index(logging.ERROR) == SETTLE_ERROR_AFTER - 1
    assert set(levels) == {logging.WARNING, logging.ERROR}


def test_a_lost_connection_on_the_release_is_retried_once(table, ray, monkeypatch):
    row = table.seed(S.RUNNING)
    real, calls = table.conn_cls.execute, []

    async def flaky(self, sql, *args):
        calls.append(sql)
        if len(calls) == 1:
            raise ConnectionError("connection lost")
        return await real(self, sql, *args)

    monkeypatch.setattr(table.conn_cls, "execute", flaky)
    run(job_lifecycle.sweep(JOB, Settled(table), T0))
    assert len(calls) == 2 and row["claimed_until"] is None  # not left leased for CLAIM_LEASE_SECONDS


def test_a_release_that_fails_twice_leaves_the_lease_to_run_out(table, ray, monkeypatch):
    row = table.seed(S.RUNNING)

    async def broken(self, sql, *args):
        raise ConnectionError("connection lost")

    monkeypatch.setattr(table.conn_cls, "execute", broken)
    run(job_lifecycle.sweep(JOB, Settled(table), T0))  # does not raise
    assert row["claimed_until"] == table.now + timedelta(seconds=CLAIM_LEASE_SECONDS)


def test_every_pod_at_pool_max_fits_in_postgres_max_connections():
    # core-api 1 + core-worker 2 (k8s apps/backend); Postgres' default max_connections, 3 of them reserved for superusers.
    pods, max_connections, reserved = 3, 100, 3
    pool_max = load("prod").pg.pool_max
    assert pods * pool_max <= max_connections - reserved and SWEEP_CONCURRENCY <= pool_max


def test_a_row_without_a_job_id_is_pending_and_never_polled(table, ray, monkeypatch):
    table.seed(S.RUNNING, ray_job_id=None)

    async def status(job_id):
        raise AssertionError("polled")

    monkeypatch.setattr(compute, "status", status)
    settle = Settled(table)
    run(job_lifecycle.sweep(JOB, settle, T0))
    assert settle.seen[0][1].ray is JobStatus.PENDING


def test_a_job_ray_gives_no_answer_for_reaches_settle_as_timed_out_once_due(table, ray):
    table.seed(S.RUNNING, deadline=T0 + timedelta(seconds=10))
    ray["job"] = RuntimeError("404 from the dashboard")
    settle = Settled(table)
    run(job_lifecycle.sweep(JOB, settle, T0))
    run(job_lifecycle.sweep(JOB, settle, T0 + timedelta(seconds=10)))
    assert [v for _, v in settle.seen] == [
        Verdict(ray=None, phase=None, timed_out=False),
        Verdict(ray=None, phase=None, timed_out=True),
    ]


def test_two_replicas_sweeping_at_once_settle_each_row_once(table, ray):
    for _ in range(SWEEP_CONCURRENCY + 4):
        table.seed(S.RUNNING)
    a, b = Settled(table, move_to=S.DONE), Settled(table, move_to=S.DONE)

    async def both():
        await asyncio.gather(job_lifecycle.sweep(JOB, a, T0), job_lifecycle.sweep(JOB, b, T0))

    run(both())
    ids = [i for i, _ in a.seen] + [i for i, _ in b.seen]
    assert len(ids) == SWEEP_CONCURRENCY + 4 == len(set(ids)) and a.seen and b.seen  # shared, not one replica doing it all


def test_a_replica_that_died_mid_settle_is_taken_over_after_the_lease(table, ray):
    row = table.seed(S.RUNNING, claimed_until=table.now + timedelta(seconds=CLAIM_LEASE_SECONDS))  # held by a dead replica
    settle = Settled(table, move_to=S.DONE)
    run(job_lifecycle.sweep(JOB, settle, T0))
    assert settle.seen == [] and row["status"] == S.RUNNING
    table.now += timedelta(seconds=CLAIM_LEASE_SECONDS + 1)
    run(job_lifecycle.sweep(JOB, settle, T0))
    assert row["status"] == S.DONE


def test_the_fake_rejects_sql_the_engine_did_not_generate(table):
    with pytest.raises(AssertionError):
        run(FakeJobConn(table).fetch("SELECT 1"))


def test_stop_quietly_never_raises(monkeypatch):
    async def gone(job_id):
        raise RuntimeError("no such job")

    monkeypatch.setattr(compute, "stop", gone)
    run(job_lifecycle.stop_quietly("job-1"))


def test_claim_sql_picks_once_in_a_materialized_cte():
    # `id IN (SELECT .. LIMIT .. FOR UPDATE SKIP LOCKED)` can plan as a nested loop that re-runs the subquery per row
    # and leases far more than LIMIT (seen against Postgres 18); the CTE is evaluated once.
    job = JobTable(name="x", table="things", tenants_fn="f", inflight=("a",), transitions={}, deadline=lambda row: None)
    sql = job_lifecycle.claim_sql(job)
    assert "WITH picked AS MATERIALIZED" in sql and "FOR UPDATE SKIP LOCKED" in sql and "id IN (" not in sql


# ---- start ----

class Code(StrEnum):
    START_FAILED = "thing_start_failed"


START_DETAIL = "the thing could not be started"


@pytest.fixture
def starting(monkeypatch):
    """compute.stop and the compensation, recorded; `fail_stop` makes the stop raise."""
    env = SimpleNamespace(calls=[], fail_stop=False)

    async def stop(job_id):
        env.calls.append(("stop", job_id))
        if env.fail_stop:
            raise RuntimeError("no such job")

    async def compensate():
        env.calls.append(("compensate",))

    monkeypatch.setattr(compute, "stop", stop)
    env.compensate = compensate
    return env


def _start(starting, step):
    return run(job_lifecycle.start(step, "job-1", starting.compensate, Code.START_FAILED, START_DETAIL, "thing t1"))


def test_start_returns_what_the_step_returns_and_undoes_nothing(starting):
    async def step():
        return "ray-1"

    assert _start(starting, step) == "ray-1"
    assert starting.calls == []


def test_a_failed_start_stops_the_job_then_compensates_once_and_becomes_a_503(starting, caplog):
    cause = RuntimeError("ray is down")

    async def step():
        raise cause

    with caplog.at_level(logging.ERROR), pytest.raises(Problem) as err:
        _start(starting, step)
    assert starting.calls == [("stop", "job-1"), ("compensate",)]
    assert (err.value.status_code, err.value.code, err.value.detail) == (503, Code.START_FAILED, START_DETAIL)
    assert err.value.__cause__ is cause
    assert any(r.exc_info and r.exc_info[1] is cause for r in caplog.records)


def test_a_failed_start_keeps_a_problem_as_it_is(starting):
    problem = Problem(409, Code.START_FAILED, "conflict")

    async def step():
        raise problem

    with pytest.raises(Problem) as err:
        _start(starting, step)
    assert err.value is problem and starting.calls == [("stop", "job-1"), ("compensate",)]


def test_a_stop_or_compensation_that_raises_does_not_hide_the_start_error(starting):
    starting.fail_stop = True

    async def broken():
        starting.calls.append(("compensate",))
        raise RuntimeError("db is down")

    async def step():
        raise RuntimeError("ray is down")

    with pytest.raises(Problem) as err:
        run(job_lifecycle.start(step, "job-1", broken, Code.START_FAILED, START_DETAIL, "thing t1"))
    assert err.value.status_code == 503 and str(err.value.__cause__) == "ray is down"
    assert starting.calls == [("stop", "job-1"), ("compensate",)]


def test_start_failed_keeps_a_problem_and_wraps_anything_else():
    problem = Problem(404, Code.START_FAILED, "missing")
    assert job_lifecycle.start_failed(problem, Code.START_FAILED, START_DETAIL, "thing t1") is problem
    cause = RuntimeError("ray is down")
    wrapped = job_lifecycle.start_failed(cause, Code.START_FAILED, START_DETAIL, "thing t1")
    assert (wrapped.status_code, wrapped.code, wrapped.__cause__) == (503, Code.START_FAILED, cause)
