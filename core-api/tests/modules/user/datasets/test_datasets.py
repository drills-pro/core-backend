"""Dataset routes: role x route matrix, 404s, pagination, audit rows, task type from the project."""
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.core.enums import TaskType
from app.core.pagination import encode_token
from app.modules.user.datasets import router as datasets
from app.modules.user.datasets import service
from app.modules.user.datasets.constants import DATASET_RETENTION_DAYS, AuditAction, EntityType
from tests.support import PROJECT, FakeConn, access, make_client, patch_tenant_conn

DS = uuid.uuid4()
NOW = datetime.now()


def ds_row(**kw):
    return {"id": DS, "name": "d", "task_type": TaskType.IMAGE_CLASSIFICATION, "created_at": NOW, "deleted_at": None,
            "published_version_id": None, **kw}


VERSION = {"id": uuid.uuid4(), "number": 1, "import_id": uuid.uuid4(), "stats": {}, "created_at": NOW}

# (method, path, json body, needs write). Every route of the module.
ROUTES = [
    ("POST", "", {"name": "x"}, True),
    ("GET", "", None, False),
    ("GET", f"/{DS}", None, False),
    ("DELETE", f"/{DS}", None, True),
    ("POST", f"/{DS}:restore", None, True),
    ("GET", f"/{DS}/versions", None, False),
    ("PUT", f"/{DS}/published", {"version_id": str(VERSION["id"])}, True),
    ("DELETE", f"/{DS}/published", None, True),
]
IDS = [f"{m} {p or '/'}".replace(str(DS), "id") for m, p, _, _ in ROUTES]


@pytest.fixture
def call(monkeypatch, dev_headers):
    client = make_client(datasets)

    def send(acc, method="GET", path="", body=None, *, row=None, missing=False, rowset=(), val=DS, qs="", executed=None):
        conn = FakeConn(acc, row=None if missing else row or ds_row(), rowset=rowset, val=val, executed=executed)
        patch_tenant_conn(monkeypatch, conn, service)
        r = client.request(method, f"/v1/projects/{PROJECT}/datasets{path}{qs}", json=body)
        r.conn = conn
        return r

    return send


@pytest.mark.parametrize("method,path,body,write", ROUTES, ids=IDS)
def test_viewer_reads_but_write_is_403_naming_the_action(call, method, path, body, write):
    r = call(access(), method, path, body)
    if write:
        assert (r.status_code, r.json()["action"]) == (403, "dataset.write")
    else:
        assert r.status_code == 200


@pytest.mark.parametrize("method,path,body,write", ROUTES, ids=IDS)
def test_dataset_editor_passes_the_write_check(call, method, path, body, write):
    assert call(access(visibility="private", roles=["dataset_editor"]), method, path, body).status_code < 300


@pytest.mark.parametrize("row", [access(visibility="private"), access(visibility=None, default_access=None, roles=None)])
@pytest.mark.parametrize("method,path,body,write", ROUTES, ids=IDS)
def test_no_access_and_cross_tenant_project_are_the_same_404(call, row, method, path, body, write):
    r = call(row, method, path, body)
    assert (r.status_code, r.json()["code"]) == (404, "not_found")


@pytest.mark.parametrize("method,path", [("GET", f"/{DS}"), ("DELETE", f"/{DS}"), ("POST", f"/{DS}:restore"), ("GET", f"/{DS}/versions"),
                                         ("PUT", f"/{DS}/published"), ("DELETE", f"/{DS}/published")])
def test_foreign_tenant_dataset_id_is_404(call, method, path):
    # RLS hides the row: every query comes back empty.
    r = call(access(roles=["editor"]), method, path, {"version_id": str(VERSION["id"])} if method == "PUT" else None, missing=True, val=None)
    assert (r.status_code, r.json()["code"]) == (404, "not_found")


def test_create_takes_task_type_from_the_project(call):
    r = call(access(roles=["editor"]), "POST", "", {"name": "x"})
    assert (r.status_code, r.json()["task_type"]) == (201, "image-classification")
    sql, args = r.conn.calls[-1]
    assert "FROM projects" in sql and str(PROJECT) in map(str, args) and len(args) == 3  # no task type is passed in


def test_create_needs_a_name(call):
    assert call(access(roles=["editor"]), "POST", "", {"name": ""}).status_code == 422


def test_list_pages_with_a_cursor(call):
    rows = [ds_row(id=uuid.uuid4(), created_at=NOW - timedelta(seconds=i)) for i in range(3)]
    r = call(access(), rowset=rows, qs="?page_size=2")
    body = r.json()
    assert len(body["items"]) == 2 and body["next_page_token"] == encode_token(rows[1]["created_at"], rows[1]["id"])
    r2 = call(access(), rowset=rows[2:], qs=f"?page_size=2&page_token={body['next_page_token']}")
    assert (len(r2.json()["items"]), r2.json()["next_page_token"]) == (1, None)
    assert "(created_at, id) <" in r2.conn.calls[-1][0]


def test_invalid_page_token_is_400(call):
    for path in ("", f"/{DS}/versions"):
        r = call(access(), path=path, qs="?page_token=garbage")
        assert (r.status_code, r.json()["code"]) == (400, "invalid_page_token")


def test_offset_paging_is_gone(call):
    sql = call(access(), qs="?offset=5").conn.calls[-1][0]
    assert "OFFSET" not in sql


def test_versions_page(call):
    r = call(access(), path=f"/{DS}/versions", rowset=[VERSION])
    assert r.json()["items"][0]["number"] == 1 and "lakefs_commit" not in r.json()["items"][0]


def audited(executed):
    return [(a[3], a[4], a[5]) for a in executed]  # action, entity_type, entity_id


def test_delete_audits_and_restore_audits(call):
    editor = access(roles=["editor"])
    executed = []
    r = call(editor, "DELETE", f"/{DS}", row=ds_row(deleted_at=NOW), executed=executed)
    assert r.status_code == 202
    r = call(editor, "POST", f"/{DS}:restore", executed=executed)
    assert r.status_code == 200
    assert audited(executed) == [(AuditAction.DELETE, EntityType.DATASET, str(DS)), (AuditAction.RESTORE, EntityType.DATASET, str(DS))]


def test_restore_of_a_live_or_expired_dataset_is_409(call):
    r = call(access(roles=["editor"]), "POST", f"/{DS}:restore", missing=True)  # update matched nothing, the dataset exists
    assert (r.status_code, r.json()["code"]) == (409, "dataset_not_deleted")
    assert DATASET_RETENTION_DAYS in r.conn.calls[-2][1]


def test_delete_cancels_the_running_imports_after_the_delete_committed(call, monkeypatch):
    order = []
    running = [{"id": uuid.uuid4()}]

    async def cancel_running(conn, dataset_id):
        order.append("cancel_rows")
        return running

    async def finish(cancelled):
        order.append(("finish", cancelled))

    monkeypatch.setattr(service.import_service, "cancel_running_imports", cancel_running)
    monkeypatch.setattr(service.import_service, "finish_cancelled", finish)
    assert call(access(roles=["editor"]), "DELETE", f"/{DS}", row=ds_row(deleted_at=NOW)).status_code == 202
    assert order == ["cancel_rows", ("finish", running)]


def test_deleted_datasets_are_hidden_from_reads(call):
    for path in ("", f"/{DS}", f"/{DS}/versions"):
        assert "deleted_at IS NULL" in call(access(), path=path).conn.calls[-1 if path != f"/{DS}/versions" else -2][0]


def test_publish_sets_the_version_scoped_to_the_dataset_and_audits(call):
    executed = []
    r = call(access(roles=["editor"]), "PUT", f"/{DS}/published", {"version_id": str(VERSION["id"])},
             row=ds_row(published_version_id=VERSION["id"]), executed=executed)
    assert (r.status_code, r.json()["published_version_id"]) == (200, str(VERSION["id"]))
    sql = r.conn.calls[-2][0]
    assert "dataset_versions WHERE id = $3 AND dataset_id = $2" in sql  # another dataset's version matches nothing
    assert audited(executed) == [(AuditAction.PUBLISH, EntityType.DATASET, str(DS))]


def test_unpublish_clears_it_and_audits(call):
    executed = []
    r = call(access(roles=["editor"]), "DELETE", f"/{DS}/published", executed=executed)
    assert (r.status_code, r.json()["published_version_id"]) == (200, None)
    assert "published_version_id = NULL" in r.conn.calls[-2][0]
    assert audited(executed) == [(AuditAction.UNPUBLISH, EntityType.DATASET, str(DS))]


def test_gallery_function_returns_metadata_and_stats_only():
    sql = next(Path(__file__).parents[4].joinpath("migrations").glob("*_dataset_versions_imports.sql")).read_text()
    returns = re.search(r"CREATE FUNCTION list_public_project_datasets.*?RETURNS TABLE\((.*?)\)", sql, re.S).group(1)
    assert [c.split()[0] for c in returns.split(",")] == ["id", "name", "published", "version_number", "stats"]
