"""GET /v1/projects/{id}/training-presets: the presets of the project's task, behind job.read; a project the caller cannot see is 404."""
import pytest

from app.modules.user.training import preset_service, routes
from tests.support import FakeConn, access, make_client, patch_tenant_conn
from tests.support.presets import preset

URL = "/v1/projects/00000000-0000-0000-0000-000000000001/training-presets"


@pytest.fixture
def call(monkeypatch, dev_headers):
    async def list_for_project(ctx, project_id):
        return [preset()]

    monkeypatch.setattr(preset_service, "list_for_project", list_for_project)

    def go(row):
        patch_tenant_conn(monkeypatch, FakeConn(row))
        return make_client(routes.presets_router).get(URL)

    return go


def test_a_viewer_lists_the_presets_of_the_task(call):
    r = call(access())
    assert r.status_code == 200
    assert [p["name"] for p in r.json()] == ["small"]


def test_a_project_the_caller_cannot_see_is_404(call):
    assert call(access(visibility="private")).status_code == 404
