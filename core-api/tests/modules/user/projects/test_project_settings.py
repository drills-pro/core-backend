"""PATCH /v1/projects/{id}/settings: project.write, merged into the stored settings; an empty patch is 400."""
import pytest

from app.modules.user.projects import routes, service
from tests.support import PROJECT, FakeConn, access, make_client, patch_tenant_conn

URL = f"/v1/projects/{PROJECT}/settings"


@pytest.fixture
def patch(monkeypatch, dev_headers):
    def go(row, body):
        patch_tenant_conn(monkeypatch, FakeConn(row, row={"id": PROJECT, "settings": {"compute_target": "gpu"}}), service)
        return make_client(routes.router).patch(URL, json=body)

    return go


def test_an_editor_merges_a_setting_and_gets_the_stored_blob(patch):
    r = patch(access(roles=["editor"]), {"compute_target": "gpu"})
    assert (r.status_code, r.json()["settings"]) == (200, {"compute_target": "gpu"})


def test_a_viewer_is_403_naming_the_action(patch):
    r = patch(access(), {"compute_target": "gpu"})
    assert (r.status_code, r.json()["action"]) == (403, "project.write")


def test_an_empty_patch_is_400_and_an_unknown_value_is_422(patch):
    assert patch(access(roles=["editor"]), {}).status_code == 400
    assert patch(access(roles=["editor"]), {"compute_target": "tpu"}).status_code == 422
