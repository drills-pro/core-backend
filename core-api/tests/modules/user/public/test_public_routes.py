"""/v1/public needs no credentials, passes its paging and project id to the SECURITY DEFINER functions, and shapes their rows."""
import uuid
from datetime import UTC, datetime

from app.modules.user.public import routes, service
from tests.support import FakeConn, make_client
from tests.support.fake_pool import patch_pool

PROJECT = uuid.uuid4()
NO_HEADERS = {}


def _client(monkeypatch, rows):
    conn = FakeConn(rowset=rows)
    pool = patch_pool(monkeypatch, service, conn)
    return make_client(routes.router, headers=NO_HEADERS), conn, pool


def test_projects_are_listed_with_the_page_asked_for_and_a_hidden_name_stays_null(monkeypatch):
    row = {"id": PROJECT, "name": None, "task_type": None, "created_at": datetime.now(UTC)}
    c, conn, pool = _client(monkeypatch, [row])
    r = c.get("/v1/public/projects?limit=5&offset=10")
    assert (r.status_code, r.json()[0]["name"]) == (200, None)
    assert conn.calls[-1][1] == (5, 10) and pool.released == 1


def test_the_page_size_is_bounded(monkeypatch):
    c, conn, _ = _client(monkeypatch, [])
    assert c.get("/v1/public/projects?limit=201").status_code == 422
    assert c.get("/v1/public/projects?offset=-1").status_code == 422
    assert not conn.calls


def test_a_projects_datasets_and_models_are_listed_by_project_id(monkeypatch):
    ds = {"id": uuid.uuid4(), "name": None, "published": False, "version_number": None, "stats": None}
    c, conn, _ = _client(monkeypatch, [ds])
    r = c.get(f"/v1/public/projects/{PROJECT}/datasets")
    assert (r.status_code, r.json()[0]["published"]) == (200, False) and conn.calls[-1][1] == (PROJECT,)
    model = {"id": uuid.uuid4(), "mlflow_model_name": None, "mlflow_version": None, "publish_metadata": False, "publish_artifact": False}
    c, conn, _ = _client(monkeypatch, [model])
    r = c.get(f"/v1/public/projects/{PROJECT}/models")
    assert (r.status_code, r.json()[0]["mlflow_model_name"]) == (200, None) and conn.calls[-1][1] == (PROJECT,)
    assert c.get("/v1/public/projects/not-a-uuid/models").status_code == 422
