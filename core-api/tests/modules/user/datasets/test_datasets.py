"""Dataset routes: role x route matrix, 404s, pagination, audit rows, task type from the project."""
import uuid
from datetime import datetime, timedelta

import asyncpg
import pytest

from app.core.enums import TaskType
from app.core.pagination import encode_token
from app.modules.user.datasets import router as datasets
from app.modules.user.datasets import service
from app.modules.user.datasets.constants import AuditAction, EntityType
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
    ("GET", f"/{DS}/versions", None, False),
]
IDS = [f"{m} {p or '/'}".replace(str(DS), "id") for m, p, _, _ in ROUTES]


@pytest.fixture
def call(monkeypatch, dev_headers):
    client = make_client(datasets)

    def send(acc, method="GET", path="", body=None, *, row=None, missing=False, rowset=(), val=DS, qs="", executed=None):
        conn = FakeConn(acc, row=None if missing else row or ds_row(), rowset=rowset, val=val, executed=executed,
                        vals={"FROM projects": TaskType.IMAGE_CLASSIFICATION})  # projects.get_task_type
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


@pytest.mark.parametrize("method,path", [("GET", f"/{DS}"), ("GET", f"/{DS}/versions")])
def test_foreign_tenant_dataset_id_is_404(call, method, path):
    # RLS hides the row: every query comes back empty.
    r = call(access(roles=["editor"]), method, path, missing=True, val=None)
    assert (r.status_code, r.json()["code"]) == (404, "not_found")


def test_create_takes_task_type_from_the_project(call):
    r = call(access(roles=["editor"]), "POST", "", {"name": "x"})
    assert (r.status_code, r.json()["task_type"]) == (201, "image-classification")
    insert, read = r.conn.calls[-3], r.conn.calls[-1]  # the audit row sits between them
    assert "FROM projects" not in insert[0] and len(insert[1]) == 3  # tenant, project, name: no task type is stored or passed in
    assert "FROM projects" in read[0] and read[1] == (PROJECT,)


def test_create_audits(call):
    executed = []
    r = call(access(roles=["editor"]), "POST", "", {"name": "x"}, executed=executed)
    assert r.status_code == 201
    assert audited(executed) == [(AuditAction.CREATE, EntityType.DATASET, str(DS))]


def test_create_needs_a_name(call):
    assert call(access(roles=["editor"]), "POST", "", {"name": ""}).status_code == 422


def test_list_pages_with_a_cursor(call):
    rows = [ds_row(id=uuid.uuid4(), created_at=NOW - timedelta(seconds=i)) for i in range(3)]
    r = call(access(), rowset=rows, qs="?page_size=2")
    body = r.json()
    assert len(body["items"]) == 2 and body["next_page_token"] == encode_token(rows[1]["created_at"], rows[1]["id"])
    r2 = call(access(), rowset=rows[2:], qs=f"?page_size=2&page_token={body['next_page_token']}")
    assert (len(r2.json()["items"]), r2.json()["next_page_token"]) == (1, None)
    assert "(created_at, id) <" in queries(r2)[-1][0]


def test_invalid_page_token_is_400(call):
    for path in ("", f"/{DS}/versions"):
        r = call(access(), path=path, qs="?page_token=garbage")
        assert (r.status_code, r.json()["code"]) == (400, "invalid_page_token")


def test_offset_paging_is_gone(call):
    sql = queries(call(access(), qs="?offset=5"))[-1][0]
    assert "OFFSET" not in sql


def test_versions_page(call):
    r = call(access(), path=f"/{DS}/versions", rowset=[VERSION])
    assert r.json()["items"][0]["number"] == 1 and "lakefs_commit" not in r.json()["items"][0]


def queries(r):
    """The dataset's own queries: the project task-type read (projects.get_task_type) is not under test here."""
    return [c for c in r.conn.calls if "FROM projects" not in c[0]]


def audited(executed):
    return [(a[3], a[4], a[5]) for a in executed]  # action, entity_type, entity_id


def test_a_project_takes_one_live_dataset_so_a_second_create_answers_409(monkeypatch, dev_headers):
    class Taken(FakeConn):
        async def fetchrow(self, sql, *args, **kwargs):
            if "INSERT INTO datasets" in sql:
                raise asyncpg.UniqueViolationError("datasets_one_live_per_project")
            return await super().fetchrow(sql, *args, **kwargs)

    patch_tenant_conn(monkeypatch, Taken(access(roles=["editor"]), vals={"FROM projects": TaskType.IMAGE_CLASSIFICATION}), service)
    r = make_client(datasets).post(f"/v1/projects/{PROJECT}/datasets", json={"name": "second"})
    assert (r.status_code, r.json()["type"]) == (409, "urn:drills:problem:project_has_dataset")
