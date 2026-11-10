"""/v1/public: the gallery needs no token and shows a private dataset or model of a public project without its name."""
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.main import create_app
from app.modules.user.public import service
from tests.support import FakeConn, TenantCtx

ID = uuid.uuid4()
PAGE = (5, 10)  # limit, offset


def get(monkeypatch, conn, path):
    async def get_pool():
        return SimpleNamespace(acquire=lambda: TenantCtx(conn))

    monkeypatch.setattr(service, "get_pool", get_pool)
    return TestClient(create_app()).get(path)  # no Authorization header at all


def test_projects_are_listed_without_a_token_and_the_page_is_passed_on(monkeypatch):
    conn = FakeConn(rowset=[{"id": ID, "name": None, "task_type": None, "created_at": datetime(2026, 11, 10, tzinfo=UTC)}])
    res = get(monkeypatch, conn, "/v1/public/projects?limit=5&offset=10")
    assert res.status_code == 200 and res.json()[0]["name"] is None and conn.calls[0][1] == PAGE


def test_a_private_dataset_exists_but_has_no_name(monkeypatch):
    rows = [{"id": ID, "name": "faces", "published": True, "version_number": 2, "stats": {"images": 9}},
            {"id": uuid.uuid4(), "name": None, "published": False, "version_number": None, "stats": None}]
    res = get(monkeypatch, FakeConn(rowset=rows), f"/v1/public/projects/{ID}/datasets")
    assert [d["name"] for d in res.json()] == ["faces", None] and res.json()[1]["stats"] is None


def test_an_unpublished_model_has_no_name_or_version(monkeypatch):
    rows = [{"id": ID, "mlflow_model_name": "model", "mlflow_version": "1", "publish_metadata": True, "publish_artifact": False},
            {"id": uuid.uuid4(), "mlflow_model_name": None, "mlflow_version": None, "publish_metadata": False, "publish_artifact": False}]
    conn = FakeConn(rowset=rows)
    res = get(monkeypatch, conn, f"/v1/public/projects/{ID}/models")
    assert res.json()[1]["mlflow_model_name"] is None and res.json()[0]["publish_artifact"] is False
    assert conn.calls[0][1] == (ID,)
