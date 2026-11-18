"""Trash: deleted datasets of the caller's projects, with the real retention as the restore deadline."""
import uuid
from datetime import datetime, timedelta

from app.modules.user.datasets import router, service, trash_router
from app.modules.user.datasets.constants import DATASET_RETENTION_DAYS
from app.modules.user.datasets.schemas import DatasetSummary
from tests.support import PROJECT, FakeConn, access, make_client, patch_tenant_conn

DELETED_AT = datetime(2026, 1, 1)
UNTIL = DELETED_AT + timedelta(days=DATASET_RETENTION_DAYS)


def test_trash_lists_deleted_datasets_with_project_and_deadline(monkeypatch, dev_headers):
    row = {"id": uuid.uuid4(), "name": "d", "created_at": DELETED_AT, "deleted_at": DELETED_AT, "project_id": PROJECT, "project_name": "p"}
    conn = FakeConn(access(), rowset=[row], val=[PROJECT])
    patch_tenant_conn(monkeypatch, conn, service)
    r = make_client(trash_router).get("/v1/datasets/trash")
    assert r.status_code == 200
    item = r.json()["items"][0]
    assert (item["project_name"], datetime.fromisoformat(item["restorable_until"])) == ("p", UNTIL)
    sql, args = next(c for c in conn.calls if "make_interval" in c[0])
    assert args[:3] == ([PROJECT], DATASET_RETENTION_DAYS, "pending_delete")


def test_a_deleted_dataset_reports_its_deadline_and_a_live_one_has_none():
    base = {"id": uuid.uuid4(), "name": "d", "task_type": None, "created_at": DELETED_AT, "published_version_id": None, "data_categories": []}
    assert DatasetSummary(**base, deleted_at=DELETED_AT).restorable_until == UNTIL
    assert DatasetSummary(**base, deleted_at=None).restorable_until is None


def test_trash_route_does_not_clash_with_the_project_scoped_ones():
    paths = {r.path for r in router.routes} | {r.path for r in trash_router.routes}
    assert "/v1/datasets/trash" in paths and "/v1/projects/{project_id}/datasets" in paths
