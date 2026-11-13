"""GET /v1/catalog/models: the enabled model_catalog rows, optionally of one task."""
import uuid

from app.core.enums import CatalogStatus
from app.modules.user.catalog import routes, service
from tests.support import FakeConn, make_client, patch_tenant_conn

ROW = {
    "id": str(uuid.uuid4()), "repo_id": "org/m", "revision": "a" * 40, "display_name": "m", "task": "image-classification",
    "license": "apache-2.0", "params": 28_000_000, "size_bytes": None,
}


def _get(monkeypatch, dev_headers, conn, **params):
    patch_tenant_conn(monkeypatch, conn, service)
    return make_client(routes.router).get("/v1/catalog/models", params=params)


def test_lists_only_enabled_entries(monkeypatch, dev_headers):
    conn = FakeConn(rowset=[ROW])
    res = _get(monkeypatch, dev_headers, conn)
    sql, args = conn.calls[-1]
    assert res.status_code == 200 and res.json() == [ROW]
    assert "FROM model_catalog" in sql and args == (CatalogStatus.ENABLED, None)


def test_the_task_filter_is_passed_to_the_query(monkeypatch, dev_headers):
    conn = FakeConn(rowset=[])
    res = _get(monkeypatch, dev_headers, conn, task="object-detection")
    assert res.status_code == 200 and res.json() == [] and conn.calls[-1][1] == (CatalogStatus.ENABLED, "object-detection")
