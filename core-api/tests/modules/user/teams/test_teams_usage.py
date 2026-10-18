"""teams + usage routes: role -> status through the real routers, database faked."""
import uuid

import pytest

from app.core.authz import TenantRole
from app.modules.user.teams import routes as teams
from app.modules.user.usage import routes as usage
from tests.support import PROJECT, FakeConn, access, make_client, patch_tenant_conn

TEAM = uuid.uuid4()


@pytest.fixture
def serve(monkeypatch, dev_headers):
    client = make_client(teams.router, usage.router)

    def build(row, team_row=None):
        patch_tenant_conn(monkeypatch, FakeConn(row, row=team_row), teams, usage)
        return client

    return build


@pytest.mark.parametrize("path", ["/v1/teams", f"/v1/teams/{TEAM}"])
def test_member_reads_teams(serve, path):
    row = dict(id=TEAM, name="t", created_at=None)
    assert serve(access(), row).get(path).status_code == 200


def test_member_cannot_create_team_403_names_action(serve):
    r = serve(access()).post("/v1/teams", params={"name": "x"})
    assert (r.status_code, r.json()["action"]) == (403, "tenant.admin")


def test_admin_creates_team(serve):
    r = serve(access(TenantRole.ADMIN), dict(id=TEAM, name="x", created_at=None)).post("/v1/teams", params={"name": "x"})
    assert r.status_code == 201


def test_missing_team_is_404(serve):
    assert serve(access(), None).get(f"/v1/teams/{TEAM}").status_code == 404


@pytest.mark.parametrize("path", ["/v1/usage", f"/v1/teams/{TEAM}/usage"])
def test_org_and_team_usage_need_billing_manage(serve, path):
    r = serve(access()).get(path)
    assert (r.status_code, r.json()["action"]) == (403, "billing.manage")
    assert serve(access(TenantRole.OWNER)).get(path).status_code == 200


def test_project_usage_viewer_reads(serve):
    assert serve(access()).get(f"/v1/projects/{PROJECT}/usage").status_code == 200


def test_project_usage_private_unbound_and_cross_tenant_are_404(serve):
    url = f"/v1/projects/{PROJECT}/usage"
    assert serve(access(visibility="private")).get(url).status_code == 404
    assert serve(dict(access(), visibility=None, default_access=None, roles=None)).get(url).status_code == 404
