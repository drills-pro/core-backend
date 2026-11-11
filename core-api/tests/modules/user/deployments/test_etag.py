"""ETag on a deployment and If-Match on stop, start and delete (planning/05 Concurrency)."""
import uuid
from datetime import UTC, datetime

import pytest

from app.core.authz import Role
from app.modules.user.deployments import etag, routes, service
from app.modules.user.deployments.constants import ETAG_HEADER, IF_MATCH_HEADER, ProblemCode
from app.modules.user.deployments.schemas import DeploymentDetail
from tests.support import PROJECT, FakeConn, access, make_client, patch_tenant_conn

DEPLOYMENT = uuid.uuid4()
BASE = f"/v1/projects/{PROJECT}/deployments/{DEPLOYMENT}"


def detail(status="active"):
    return DeploymentDetail(id=DEPLOYMENT, status=status, route_prefix="/p", created_at=datetime.now(UTC),
                            project_id=PROJECT, training_run_id=uuid.uuid4(), rate_limit_per_minute=10)


def test_the_tag_changes_with_the_state_and_matches_itself_in_any_spelling():
    d = detail()
    stopped = d.model_copy(update={"status": "stopped"})
    assert etag.etag_of(d) == etag.etag_of(d) != etag.etag_of(stopped)
    for header in (etag.etag_of(d), f"W/{etag.etag_of(d)}", f'"x", {etag.etag_of(d)}', "*"):
        etag.check_if_match(header, d)
    etag.check_if_match(None, d)


def test_a_stale_tag_is_412():
    d = detail()
    with pytest.raises(Exception) as e:
        etag.check_if_match(etag.etag_of(d.model_copy(update={"status": "stopped"})), d)
    assert e.value.status_code == 412 and e.value.code == ProblemCode.PRECONDITION_FAILED


@pytest.fixture
def call(monkeypatch, dev_headers):
    state = {"d": detail()}

    async def get(ctx, project_id, deployment_id):
        return state["d"]

    async def stop(ctx, project_id, deployment_id):
        state["d"] = state["d"].model_copy(update={"status": "stopped"})
        return state["d"]

    monkeypatch.setattr(service, "get_deployment", get)
    monkeypatch.setattr(service, "stop_deployment", stop)
    client = make_client(routes.router, raise_server_exceptions=False)

    def go(method, path, **headers):
        patch_tenant_conn(monkeypatch, FakeConn(access(roles=[Role.DEPLOYER])), service)
        return getattr(client, method)(path, headers=headers)

    return go, state


def test_get_returns_the_tag_and_a_stop_with_it_succeeds_and_returns_the_new_one(call):
    go, state = call
    tag = go("get", BASE).headers[ETAG_HEADER]
    assert tag == etag.etag_of(state["d"])
    r = go("post", f"{BASE}/stop", **{IF_MATCH_HEADER: tag})
    assert r.status_code == 200 and r.headers[ETAG_HEADER] == etag.etag_of(state["d"]) != tag


def test_a_stop_with_a_stale_tag_is_412_and_changes_nothing(call):
    go, state = call
    stale = go("get", BASE).headers[ETAG_HEADER]
    assert go("post", f"{BASE}/stop").status_code == 200  # someone else stopped it
    state["d"] = state["d"].model_copy(update={"status": "active"})
    r = go("post", f"{BASE}/stop", **{IF_MATCH_HEADER: etag.etag_of(state["d"].model_copy(update={"status": "stopped"}))})
    assert r.status_code == 412 and state["d"].status == "active"
    assert stale  # the first read had a tag


def test_delete_with_a_stale_tag_is_412(call):
    go, _ = call
    assert go("delete", BASE, **{IF_MATCH_HEADER: '"stale"'}).status_code == 412
