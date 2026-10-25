"""Cancel goes through the shared transition(), not a second UPDATE path."""
import uuid

import asyncio

import pytest
from fastapi import HTTPException

from types import SimpleNamespace
from app.modules.user.training import run_service
from tests.support import FakeConn, patch_tenant_conn

CTX = SimpleNamespace(tenant_id=str(uuid.uuid4()))


def _row(status):
    return {"status": status, "ray_job_id": "job-1"}


def test_cancel_rejects_a_finished_run(monkeypatch):
    patch_tenant_conn(monkeypatch, FakeConn(row=_row("succeeded")), run_service)
    with pytest.raises(HTTPException) as e:
        asyncio.run(run_service.cancel_run(CTX, uuid.uuid4(), uuid.uuid4()))
    assert e.value.status_code == 409


def test_cancel_stops_the_job_then_transitions_to_cancelled(monkeypatch):
    seen = {}
    patch_tenant_conn(monkeypatch, FakeConn(row=_row("running")), run_service)

    async def stop(job_id):
        seen["stopped"] = job_id

    async def transition(conn, row, new_status, tenant_id):
        seen["transition"] = new_status
        return {"sentinel": True}

    monkeypatch.setattr(run_service.compute, "stop", stop)
    monkeypatch.setattr(run_service.service, "transition", transition)
    monkeypatch.setattr(run_service.TrainingRun, "model_validate", staticmethod(lambda d: d))
    out = asyncio.run(run_service.cancel_run(CTX, uuid.uuid4(), uuid.uuid4()))
    assert (seen, out) == ({"stopped": "job-1", "transition": "cancelled"}, {"sentinel": True})
