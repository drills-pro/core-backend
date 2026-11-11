"""GET /v1/projects/{id}/events: the project's job state changes by cursor, behind job.read; a project the caller cannot see is 404."""
import datetime as dt
import uuid

import pytest

from app.modules.user.job_events import routes, service
from tests.support import FakeConn, access, make_client, patch_tenant_conn

URL = "/v1/projects/00000000-0000-0000-0000-000000000001/events"
ROW = {"seq": 7, "kind": "training.running", "entity_id": uuid.uuid4(), "status": "running", "occurred_at": dt.datetime(2026, 11, 12, tzinfo=dt.UTC)}


@pytest.fixture
def call(monkeypatch, dev_headers):
    seen = {}

    class Conn(FakeConn):
        async def fetch(self, sql, *a):
            assert sql == service._LIST
            seen["args"] = a
            return [ROW]

    def go(row, query=""):
        patch_tenant_conn(monkeypatch, Conn(row), service)
        r = make_client(routes.router).get(URL + query)
        return r, seen

    return go


def test_a_viewer_reads_events_after_a_cursor(call):
    r, seen = call(access(), "?after=3&limit=10")
    assert r.status_code == 200
    assert [(e["seq"], e["kind"]) for e in r.json()] == [(7, "training.running")]
    assert seen["args"][1:] == (3, 10)


def test_a_project_the_caller_cannot_see_is_404(call):
    assert call(access(visibility="private"))[0].status_code == 404
