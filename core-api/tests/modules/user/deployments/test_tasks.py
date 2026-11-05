"""The deployments reconciler: a deploy job's Ray state decides the row, a compare-and-set moves it, and a job Ray
no longer answers for fails at its deadline instead of staying 'deploying' forever."""
import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.integrations import compute
from app.integrations.compute import JobStatus
from app.modules.user.deployments import tasks
from app.modules.user.deployments.status import DeploymentStatus
from tests.support.job_table import FakeJobTable, patch

S, R = DeploymentStatus, JobStatus
T0 = datetime(2026, 1, 1, tzinfo=UTC)
DEADLINE = timedelta(seconds=tasks.DEPLOY_DEADLINE_SECONDS)


@pytest.fixture
def env(monkeypatch):
    table, ray = FakeJobTable(tasks.DEPLOYMENTS), SimpleNamespace(answer=R.RUNNING, stopped=[], polled=[])
    patch(monkeypatch, table, tasks)

    async def status(job_id):
        ray.polled.append(job_id)
        if isinstance(ray.answer, Exception):
            raise ray.answer
        return ray.answer

    async def stop(job_id):
        ray.stopped.append(job_id)

    monkeypatch.setattr(compute, "status", status)
    monkeypatch.setattr(compute, "stop", stop)
    return SimpleNamespace(table=table, ray=ray)


def deploying(env, **kw):
    return env.table.seed(S.DEPLOYING, deploying_since=kw.pop("since", datetime.now(UTC)), **kw)


def reconcile():
    asyncio.run(tasks.reconcile_inflight_deployments())


def test_a_job_still_starting_leaves_the_deployment_deploying(env):
    row = deploying(env)
    reconcile()
    assert row["status"] == S.DEPLOYING and row["claimed_until"] is None


@pytest.mark.parametrize("answer,expected", [
    (R.SUCCEEDED, S.ACTIVE),
    (R.FAILED, S.FAILED),
    (R.STOPPED, S.FAILED),  # a stopped deploy job never serves; it used to stay 'deploying' forever
])
def test_a_finished_job_settles_the_deployment(env, answer, expected):
    row = deploying(env)
    env.ray.answer = answer
    reconcile()
    assert row["status"] == expected and env.ray.stopped == []


def test_a_job_ray_garbage_collected_stays_deploying_until_the_deadline_then_fails_and_is_stopped(env):
    fresh = deploying(env)
    old = deploying(env, since=datetime.now(UTC) - DEADLINE - timedelta(seconds=1))
    env.ray.answer = RuntimeError("404 from the dashboard")
    reconcile()
    assert (fresh["status"], old["status"]) == (S.DEPLOYING, S.FAILED)
    assert env.ray.stopped == [old["ray_job_id"]]


def test_a_job_still_running_past_the_deadline_fails(env):
    row = deploying(env, since=datetime.now(UTC) - DEADLINE)
    reconcile()
    assert row["status"] == S.FAILED and env.ray.stopped == ["job"]


def test_a_job_that_finished_wins_even_past_the_deadline(env):
    row = deploying(env, since=datetime.now(UTC) - 2 * DEADLINE)
    env.ray.answer = R.SUCCEEDED
    reconcile()
    assert row["status"] == S.ACTIVE and env.ray.stopped == []


def test_a_deployment_deleted_while_the_job_was_polled_stays_deleted(env, monkeypatch):
    row = deploying(env)
    env.ray.answer = R.SUCCEEDED
    original = compute.status

    async def status_then_delete(job_id):
        answer = await original(job_id)
        row["status"] = S.DELETED  # the user deleted it between the poll and the write
        return answer

    monkeypatch.setattr(compute, "status", status_then_delete)
    reconcile()
    assert row["status"] == S.DELETED  # the old bare UPDATE would have brought it back to 'active'


def test_a_failing_stop_does_not_undo_the_timeout(env, monkeypatch):
    row = deploying(env, since=datetime.now(UTC) - DEADLINE)

    async def stop(job_id):
        raise RuntimeError("head is down")

    monkeypatch.setattr(compute, "stop", stop)
    reconcile()
    assert row["status"] == S.FAILED


def test_the_table_has_no_completed_at_and_only_deploying_rows_are_followed(env):
    assert tasks.DEPLOYMENTS.completed_col is None and tasks.DEPLOYMENTS.inflight == (S.DEPLOYING,)
    done = env.table.seed(S.ACTIVE, deploying_since=T0)
    reconcile()
    assert done["status"] == S.ACTIVE


def test_two_replicas_share_the_deployments_without_polling_one_twice(env):
    rows = [deploying(env) for _ in range(6)]
    env.ray.answer = R.SUCCEEDED

    async def both():
        await asyncio.gather(tasks.reconcile_inflight_deployments(), tasks.reconcile_inflight_deployments())

    asyncio.run(both())
    assert {r["status"] for r in rows} == {S.ACTIVE} and len(env.ray.polled) == 6  # each row polled by one replica only
