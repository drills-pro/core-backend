"""Training routes: job.read / job.create / job.cancel through the real router, database faked."""
import uuid

import pytest

from app.core.authz import TenantRole
from app.modules.user.training import routes, run_service
from tests.support import PROJECT, FakeConn, access, make_client, patch_tenant_conn

BASE = f"/v1/projects/{PROJECT}/training-runs"
RUN = uuid.uuid4()


@pytest.fixture
def call(monkeypatch, dev_headers):
    client = make_client(routes.router)

    def go(method, path, row, **kw):
        patch_tenant_conn(monkeypatch, FakeConn(row, row=row), run_service)
        return client.request(method, path, **kw)

    return go


def test_viewer_lists_runs(call):
    assert call("GET", BASE, access()).status_code == 200


@pytest.mark.parametrize(
    "method,path,kw,action",
    [
        ("POST", f"{BASE}?dataset_ref=x", {"headers": {"Idempotency-Key": "k"}}, "job.create"),
        ("POST", f"{BASE}/{RUN}/cancel", {}, "job.cancel"),
    ],
)
def test_viewer_denied_403_names_action(call, method, path, kw, action):
    r = call(method, path, access(), **kw)
    assert r.status_code == 403 and r.json()["action"] == action


def test_private_unbound_is_404(call):
    assert call("GET", BASE, access(visibility="private")).status_code == 404


def test_cross_tenant_project_is_404(call):
    assert call("GET", f"{BASE}/{RUN}", access(visibility=None, default_access=None, roles=None)).status_code == 404


def test_create_is_a_501_stub_until_the_trainer_slice(call):
    r = call("POST", f"{BASE}?dataset_ref=x", access(role=TenantRole.ADMIN), headers={"Idempotency-Key": "k"})
    assert (r.status_code, r.json()["code"]) == (501, "not_implemented")


def test_a_finished_run_cannot_move_again():
    from app.modules.user.training.constants import TRANSITIONS, TrainingStatus

    assert TrainingStatus.RUNNING not in TRANSITIONS[TrainingStatus.SUCCEEDED]
    assert TrainingStatus.SUBMITTED not in TRANSITIONS[TrainingStatus.RUNNING]
    assert all(not TRANSITIONS[s] for s in (TrainingStatus.SUCCEEDED, TrainingStatus.FAILED, TrainingStatus.CANCELLED))


class _Conn:
    """Answers the row lock with the run and records the UPDATE's arguments."""

    def __init__(self, row, lose_the_race=False):
        self.row, self.update_args, self.lose = row, None, lose_the_race

    async def fetchrow(self, sql, *args):
        if "FOR UPDATE" in sql:
            return self.row
        self.update_args = args
        if self.lose:
            return None  # another writer moved the row between the lock and the UPDATE
        return {**self.row, "status": args[1]}


def _run_row(status):
    import datetime

    return {"id": uuid.uuid4(), "status": status, "project_id": uuid.uuid4(), "mlflow_run_id": None,
            "ray_job_id": "job-1", "created_at": datetime.datetime.now(datetime.UTC)}


def test_a_cancelled_run_gets_a_completed_at():
    import asyncio

    from app.modules.user.training import service
    from app.modules.user.training.constants import TrainingStatus

    conn = _Conn(_run_row("running"))
    asyncio.run(service.transition(conn, dict(conn.row), TrainingStatus.CANCELLED, "t1"))
    assert conn.update_args[3] is not None  # completed_at


def test_the_sweep_ignores_a_run_someone_else_just_moved(monkeypatch):
    import asyncio

    from app.integrations.compute import JobStatus
    from app.modules.user.training import service
    from app.modules.user.training.constants import TrainingStatus

    async def poll(job_id):
        return JobStatus.SUCCEEDED

    async def finish_run(*a, **k):
        return None

    async def logs(job_id):
        return ""

    conn = _Conn(_run_row("running"), lose_the_race=True)
    monkeypatch.setattr(service.job_follower, "poll", poll)
    monkeypatch.setattr(service.compute, "logs", logs)
    row = {**conn.row, "status": TrainingStatus.RUNNING, "tenant_id": uuid.uuid4()}
    asyncio.run(service._settle(conn, row))  # no exception: the 409 is the expected race
