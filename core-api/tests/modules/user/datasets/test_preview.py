"""Preview: sample rows plus presigned URLs from the parquet index, no lakeFS listing, foreign ids are 404."""
import asyncio
import io
import uuid

import pytest

from app.core import secret_files
from app.core.enums import TaskType
from app.integrations.data_plane import access, layout, lakefs
from app.modules.user.datasets import preview_service
from app.modules.user.datasets import router as datasets
from app.modules.user.datasets.constants import ImportStatus
from tests.support import PROJECT, TENANT, FakeConn, TenantCtx, access as access_row, make_client, patch_tenant_conn

DS, VER, IMP = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
BASE = f"/v1/projects/{PROJECT}/datasets/{DS}"
IMAGE_ROWS = [{"path": "cat/1.jpg", "label": "cat"}, {"path": "dog/2.jpg", "label": "dog"}]
TABLE_ROWS = [{"a": 1, "y": 0}, {"a": 2, "y": 1}]


class FakeGateway:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    async def read_version_sample(self, tenant_id, dataset_id, version_id, key, limit):
        self.calls.append(("read_version_sample", key, limit))
        return self.rows[:limit]

    async def read_staged_sample(self, tenant_id, dataset_id, import_id, key, limit):
        self.calls.append(("read_staged_sample", key, limit))
        return self.rows[:limit]

    async def presign_get(self, tenant_id, dataset_id, version_id, path):
        self.calls.append(("presign_get", path))
        return f"https://files.test/{path}"

    async def presign_gets(self, tenant_id, dataset_id, version_id, paths):
        self.calls.append(("presign_gets", paths))
        return {p: f"https://files.test/{p}" for p in paths}


@pytest.fixture
def call(monkeypatch, dev_headers):
    client = make_client(datasets)

    def send(path, *, acc=None, task=TaskType.IMAGE_CLASSIFICATION, rows=IMAGE_ROWS, found=True, import_status=ImportStatus.AWAITING_REVIEW):
        vals = {"FROM datasets": 1 if found else None, "FROM projects": task, "FROM dataset_versions": 1 if found else None,
                "FROM dataset_imports": import_status if found else None}
        conn = FakeConn(acc or access_row(roles=["editor"]), vals=vals)
        patch_tenant_conn(monkeypatch, conn, preview_service)
        gw = FakeGateway(rows)
        monkeypatch.setattr(preview_service, "data_plane", gw)
        r = client.get(BASE + path)
        r.gw = gw
        return r

    return send


def test_version_preview_returns_rows_with_urls_and_reads_only_the_index(call):
    r = call(f"/versions/{VER}/preview")
    assert r.status_code == 200
    assert r.json()["rows"] == [
        {"data": IMAGE_ROWS[0], "url": "https://files.test/objects/cat/1.jpg"},
        {"data": IMAGE_ROWS[1], "url": "https://files.test/objects/dog/2.jpg"},
    ]
    assert [c[0] for c in r.gw.calls] == ["read_version_sample", "presign_gets"]  # one batch presign; no listing operation exists on the gateway
    assert r.gw.calls[0] == ("read_version_sample", layout.INDEX_KEY, 20)


def test_tabular_preview_reads_data_parquet_and_has_no_urls(call):
    r = call(f"/versions/{VER}/preview?limit=1", task=TaskType.TABULAR_CLASSIFICATION, rows=TABLE_ROWS)
    assert r.json() == {"rows": [{"data": TABLE_ROWS[0], "url": None}]}
    assert r.gw.calls == [("read_version_sample", layout.DATA_KEY, 1)]


@pytest.mark.parametrize("limit", [0, 101, "x"])
def test_limit_is_bounded(call, limit):
    assert call(f"/versions/{VER}/preview?limit={limit}").status_code == 422





def test_import_preview_reads_the_staged_index(call):
    r = call(f"/imports/{IMP}/preview")
    assert r.status_code == 200 and r.json()["rows"][0] == {"data": IMAGE_ROWS[0], "url": None}
    assert r.gw.calls == [("read_staged_sample", layout.INDEX_KEY, 20)]


def test_import_preview_outside_review_is_409(call):
    r = call(f"/imports/{IMP}/preview", import_status=ImportStatus.RUNNING)
    assert (r.status_code, r.json()["code"]) == (409, "invalid_import_state")


@pytest.mark.parametrize("path", [f"/versions/{VER}/preview", f"/imports/{IMP}/preview"])
def test_foreign_or_missing_dataset_version_import_is_404(call, path):
    r = call(path, found=False)
    assert (r.status_code, r.json()["code"]) == (404, "not_found") and not r.gw.calls


# Gateway side: ownership first, then only the parquet head is read (no lakeFS API, no listing).
@pytest.fixture
def gateway(monkeypatch):
    seen = []

    def boom(*a, **kw):
        raise AssertionError("lakeFS API or listing reached")

    monkeypatch.setattr(lakefs, "_lakefs_api", boom)
    monkeypatch.setattr(lakefs, "_s3", boom)
    monkeypatch.setattr(lakefs, "_parquet_head", lambda secret, location, limit: seen.append((secret, location, limit)) or IMAGE_ROWS)
    return seen


def test_gateway_version_sample_reads_the_index_at_the_commit(gateway, monkeypatch):
    monkeypatch.setattr(access, "TenantConn", lambda t: TenantCtx(FakeConn(val="c0ffee")))
    rows = asyncio.run(lakefs.read_version_sample(TENANT, str(DS), str(VER), layout.INDEX_KEY, 5))
    assert rows == IMAGE_ROWS
    assert gateway == [(secret_files.LAKEFS, f"{layout.repo_name(str(DS))}/c0ffee/{layout.INDEX_KEY}", 5)]


def test_gateway_staged_sample_reads_out_index(gateway, monkeypatch):
    monkeypatch.setattr(access, "TenantConn", lambda t: TenantCtx(FakeConn(val=1)))
    asyncio.run(lakefs.read_staged_sample(TENANT, str(DS), str(IMP), layout.INDEX_KEY, 5))
    (secret, location, _), = gateway
    assert secret == secret_files.STAGING and location.endswith(f"{layout.staging_prefix(TENANT, str(DS), str(IMP))}out/index.parquet")


PREVIEW_PATHS = [f"/versions/{VER}/preview", f"/imports/{IMP}/preview"]


@pytest.mark.parametrize("path", PREVIEW_PATHS)
def test_viewer_can_read_preview_routes(call, path):
    assert call(path, acc=access_row()).status_code == 200


@pytest.mark.parametrize("path", PREVIEW_PATHS)
def test_caller_without_project_access_gets_404_and_no_backend_call(call, path):
    r = call(path, acc=access_row(visibility="private"))
    assert (r.status_code, r.json()["code"]) == (404, "not_found") and not r.gw.calls


def test_parquet_head_reads_only_the_first_rows_from_bucket_key_location(monkeypatch, tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq

    (tmp_path / "bucket/c0ffee").mkdir(parents=True)
    path = tmp_path / "bucket/c0ffee/index.parquet"
    pq.write_table(pa.table({"path": [f"p{i}" for i in range(50)]}), path, row_group_size=10)
    data, ranges = path.read_bytes(), []

    class FakeS3:
        def head_object(self, Bucket, Key):
            return {"ContentLength": len(data)}

        def get_object(self, Bucket, Key, Range):
            assert (Bucket, Key) == ("bucket", "c0ffee/index.parquet")
            lo, hi = map(int, Range.removeprefix("bytes=").split("-"))
            ranges.append((lo, hi))
            return {"Body": io.BytesIO(data[lo : hi + 1])}

    monkeypatch.setattr(lakefs, "_s3", lambda secret, endpoint=None: FakeS3())
    rows = lakefs._parquet_head(secret_files.LAKEFS, "bucket/c0ffee/index.parquet", 3)
    assert rows == [{"path": "p0"}, {"path": "p1"}, {"path": "p2"}]
    assert ranges and all(hi < len(data) for _, hi in ranges)
