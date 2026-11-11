"""GET /v1/projects/{id}/notifications and POST .../read: a member's unread job events, behind job.read; the cursor only moves forward."""
import datetime as dt
import uuid

import pytest

from app.modules.user.notifications import routes, service
from tests.support import PROJECT, FakeConn, access, make_client, patch_tenant_conn

URL = f"/v1/projects/{PROJECT}/notifications"
ROW = {"seq": 7, "kind": "training.succeeded", "entity_id": uuid.uuid4(), "status": "succeeded", "occurred_at": dt.datetime(2026, 11, 12, tzinfo=dt.UTC)}


@pytest.fixture
def call(monkeypatch, dev_headers):
    seen = []

    class Conn(FakeConn):
        async def fetchval(self, sql, *a):
            return 3

        async def fetch(self, sql, *a):
            seen.append(("fetch", sql, a))
            return [ROW]

        async def execute(self, sql, *a):
            seen.append(("execute", sql, a))

    def go(method, path, row):
        patch_tenant_conn(monkeypatch, Conn(row), service)
        return make_client(routes.router).request(method, URL + path, json={"seq": 9} if method == "POST" else None), seen

    return go


def test_a_viewer_reads_the_unread_count_and_the_newest_events(call):
    r, _ = call("GET", "", access())
    assert r.status_code == 200
    assert (r.json()["unread"], [e["seq"] for e in r.json()["items"]]) == (3, [7])


def test_marking_read_moves_the_cursor_with_greatest_so_it_never_goes_back(call):
    r, seen = call("POST", "/read", access())
    assert r.status_code == 204
    assert "GREATEST" in seen[0][1] and seen[0][2][3] == 9


def test_a_project_the_caller_cannot_see_is_404(call):
    assert call("GET", "", access(visibility="private"))[0].status_code == 404
