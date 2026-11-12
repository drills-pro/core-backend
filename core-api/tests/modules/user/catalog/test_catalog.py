"""GET /v1/catalog/models: the presets grouped by base model."""
from app.modules.user.catalog import routes, service
from tests.support import FakeConn, make_client, patch_tenant_conn

ROW = {"repo_id": "org/m", "revision": "a" * 40, "license": "apache-2.0", "tasks": ["image-classification"], "presets": ["small", "tiny"]}


def test_lists_each_base_model_once_with_its_tasks_and_presets(monkeypatch, dev_headers):
    conn = FakeConn(rowset=[ROW])
    patch_tenant_conn(monkeypatch, conn, service)
    res = make_client(routes.router).get("/v1/catalog/models")
    assert res.status_code == 200 and res.json() == [ROW]
    assert "GROUP BY" in conn.calls[-1][0]
