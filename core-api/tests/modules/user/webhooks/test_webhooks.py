"""Webhook endpoints (routes, role -> status, database faked) and the dispatcher's delivery, signing and backoff."""
import asyncio
import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime

import httpx
import pytest

from app.modules.user.job_events import JobEvent
from app.modules.user.webhooks import constants as c
from app.modules.user.webhooks import dispatcher, routes, service, signing
from tests.support import PROJECT, FakeConn, access, make_client, patch_tenant_conn

BASE = f"/v1/projects/{PROJECT}/webhooks"
HOOK_ID = uuid.uuid4()
NOW = datetime.now(UTC)
URL = "https://example.com/hook"
EVENT = JobEvent(seq=7, kind="training.succeeded", entity_id=uuid.uuid4(), status="succeeded", occurred_at=NOW)


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setattr(signing, "master_key", lambda: b"master")


@pytest.fixture
def call(monkeypatch, dev_headers):
    client = make_client(routes.router)

    def go(method, path="", row=None, **kw):
        conn = FakeConn(row or access(roles=["deployer"]), row={"id": HOOK_ID, "created_at": NOW}, val=HOOK_ID,
                        rowset=[{"id": HOOK_ID, "url": URL, "created_at": NOW, "disabled_at": None}])
        patch_tenant_conn(monkeypatch, conn, service)
        go.calls = conn.calls
        return client.request(method, BASE + path, **kw)

    return go


def test_a_viewer_gets_403_and_a_deployer_may_create_list_and_delete(call):
    assert call("GET", row=access()).status_code == 403
    assert call("POST", json={"url": URL}).status_code == 201
    assert call("GET").json()[0]["url"] == URL
    assert call("DELETE", f"/{HOOK_ID}").status_code == 204


def test_the_secret_is_derived_from_the_endpoint_id_and_shown_in_the_create_response(call):
    body = call("POST", json={"url": URL}).json()
    assert body["secret"] == hmac.new(b"master", str(HOOK_ID).encode(), hashlib.sha256).hexdigest()


@pytest.mark.parametrize("url", ["http://example.com/h", "https://localhost/h", "https://10.0.0.5/h", "https://169.254.169.254/",
                                 "https://api.platform.svc.cluster.local/h", "https://u:p@example.com/h", "https:///h"])
def test_a_url_that_is_not_a_public_https_host_is_422(call, url):
    r = call("POST", json={"url": url})
    assert (r.status_code, r.json()["code"]) == (422, c.ProblemCode.BAD_URL)


def test_creating_without_the_signing_key_is_503(call, monkeypatch):
    def missing():
        raise signing.SigningUnavailable("no file")

    monkeypatch.setattr(signing, "master_key", missing)
    assert call("POST", json={"url": URL}).status_code == 503


def test_deleting_an_unknown_endpoint_is_404(monkeypatch, dev_headers):
    patch_tenant_conn(monkeypatch, FakeConn(access(roles=["deployer"]), val=None), service)
    assert make_client(routes.router).delete(f"{BASE}/{HOOK_ID}").status_code == 404


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_a_delivery_is_signed_over_the_timestamp_and_body():
    asyncio.run(_test_a_delivery_is_signed_over_the_timestamp_and_body())


async def _test_a_delivery_is_signed_over_the_timestamp_and_body():
    seen = {}

    def handler(request):
        seen.update(dict(request.headers), body=request.content)
        return httpx.Response(204)

    async with _client(handler) as client:
        assert await dispatcher.deliver(client, "s3cret", URL, PROJECT, EVENT)
    ts = int(seen[c.TIMESTAMP_HEADER.lower()])
    assert seen[c.SIGNATURE_HEADER.lower()] == signing.sign("s3cret", ts, seen["body"])
    assert (seen[c.EVENT_ID_HEADER.lower()], json.loads(seen["body"])["kind"]) == ("7", "training.succeeded")


def test_a_non_2xx_or_a_network_error_is_a_failed_delivery():
    asyncio.run(_test_a_non_2xx_or_a_network_error_is_a_failed_delivery())


async def _test_a_non_2xx_or_a_network_error_is_a_failed_delivery():
    async with _client(lambda r: httpx.Response(500)) as client:
        assert not await dispatcher.deliver(client, "s", URL, PROJECT, EVENT)

    def boom(request):
        raise httpx.ConnectError("down")

    async with _client(boom) as client:
        assert not await dispatcher.deliver(client, "s", URL, PROJECT, EVENT)


def test_backoff_doubles_and_is_capped():
    assert [dispatcher.backoff(n) for n in (1, 2, 3)] == [30, 60, 120]
    assert dispatcher.backoff(99) == c.BACKOFF_MAX_SECONDS


class _Conn:
    def __init__(self, rows):
        self.rows, self.writes = rows, []

    async def fetch(self, sql, *a):
        return self.rows

    async def execute(self, sql, *a):
        self.writes.append((sql, a))


@pytest.fixture
def send(monkeypatch):
    """Runs one endpoint's tick; returns the conn's writes. `ok` says what each delivery answers."""
    def go(statuses):
        conn, answers = _Conn([{"seq": 8, "kind": "k", "entity_id": uuid.uuid4(), "status": "s", "occurred_at": NOW},
                               {"seq": 9, "kind": "k", "entity_id": uuid.uuid4(), "status": "s", "occurred_at": NOW}]), iter(statuses)

        class TC:
            def __init__(self, tenant): pass
            async def __aenter__(self): return conn
            async def __aexit__(self, *e): return False

        async def public(host):
            return True

        monkeypatch.setattr(dispatcher, "TenantConn", TC)
        monkeypatch.setattr(dispatcher, "_public_host", public)
        client = _client(lambda r: httpx.Response(next(answers)))
        ep = {"id": HOOK_ID, "project_id": PROJECT, "url": URL, "last_seq": 7, "failures": 0}
        return conn, client, ep

    return go


def test_the_cursor_moves_past_what_the_receiver_accepted(send):
    asyncio.run(_test_the_cursor_moves_past_what_the_receiver_accepted(send))


async def _test_the_cursor_moves_past_what_the_receiver_accepted(send):
    conn, client, ep = send([200, 200])
    async with client:
        await dispatcher._send_endpoint(client, "t", ep)
    assert [(w[0], w[1][2]) for w in conn.writes] == [(dispatcher._MOVE, 9)]


def test_a_failure_keeps_the_cursor_at_the_last_accepted_event_and_backs_off(send):
    asyncio.run(_test_a_failure_keeps_the_cursor_at_the_last_accepted_event_and_backs_off(send))


async def _test_a_failure_keeps_the_cursor_at_the_last_accepted_event_and_backs_off(send):
    conn, client, ep = send([200, 500])
    async with client:
        await dispatcher._send_endpoint(client, "t", ep)
    assert [w[0] for w in conn.writes] == [dispatcher._MOVE, dispatcher._FAIL]
    assert conn.writes[0][1][2] == 8 and conn.writes[1][1][1:] == (30, c.DISABLE_AFTER_FAILURES)


def test_a_host_that_resolves_inside_the_cluster_is_never_called(send, monkeypatch):
    asyncio.run(_test_a_host_that_resolves_inside_the_cluster_is_never_called(send, monkeypatch))


async def _test_a_host_that_resolves_inside_the_cluster_is_never_called(send, monkeypatch):
    conn, client, ep = send([])

    async def private(host):
        return False

    monkeypatch.setattr(dispatcher, "_public_host", private)
    async with client:
        await dispatcher._send_endpoint(client, "t", ep)
    assert [w[0] for w in conn.writes] == [dispatcher._FAIL]
