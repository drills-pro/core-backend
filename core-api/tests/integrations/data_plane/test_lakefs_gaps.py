"""lakefs: health, the parquet readers, staged listings and purges, and the error paths the happy-path gateway tests skip."""
import asyncio
import io
import logging
import uuid

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from botocore.exceptions import ClientError

from app.core.problem import Problem
from app.core.settings import settings
from app.integrations.data_plane import access, layout, lakefs
from app.integrations.data_plane.report import Outcome
from tests.support import FakeConn, TenantCtx

TENANT, DATASET, IMPORT, VERSION = (str(uuid.uuid4()) for _ in range(4))
COMMIT = "c0ffee"
CREDS = {"endpoint": "http://lf.test", "access_key": "k", "secret_key": "s"}


@pytest.fixture
def owned(monkeypatch):
    monkeypatch.setattr(access, "TenantConn", lambda tenant_id: TenantCtx(FakeConn(val=1, vals={"lakefs_commit": COMMIT})))
    monkeypatch.setattr(lakefs.secret_files, "get", lambda name: CREDS)


def parquet_bytes(rows):
    sink = io.BytesIO()
    pq.write_table(pa.table({"path": [r["path"] for r in rows], "label": [r["label"] for r in rows]}), sink)
    return sink.getvalue()


class S3:
    """Ranged GETs over `objects` ({key: bytes}), the way boto3 answers them, plus the calls the staging code makes."""

    def __init__(self, objects=None, pages=()):
        self.objects, self.pages, self.ranges, self.deleted = objects or {}, list(pages), [], []

    def head_object(self, Bucket, Key):
        return {"ContentLength": len(self.objects[Key])}

    def get_object(self, Bucket, Key, Range=None):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        data = self.objects[Key]
        if Range:
            self.ranges.append(Range)
            start, end = (int(n) for n in Range.removeprefix("bytes=").split("-"))
            data = data[start : end + 1]
        return {"Body": io.BytesIO(data)}

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return self

    def paginate(self, Bucket, Prefix):
        self.listed = (Bucket, Prefix)
        return iter(self.pages)

    def delete_objects(self, Bucket, Delete):
        self.deleted.append((Bucket, [o["Key"] for o in Delete["Objects"]]))

    def list_buckets(self):
        return {}


ROWS = [{"path": f"cat/{i}.jpg", "label": "cat"} for i in range(5)]


def use(monkeypatch, s3):
    monkeypatch.setattr(lakefs, "_s3", lambda secret, endpoint=None: s3)
    return s3


def test_the_lakefs_client_is_scoped_to_the_api_with_basic_auth(monkeypatch):
    monkeypatch.setattr(lakefs.secret_files, "get", lambda name: CREDS)
    client = lakefs._lakefs_api()
    assert str(client.base_url) == "http://lf.test/api/v1/"
    assert client._auth._auth_header == httpx.BasicAuth("k", "s")._auth_header


def test_health_is_ok_when_both_stores_answer(monkeypatch):
    used = []
    monkeypatch.setattr(lakefs, "_s3", lambda secret, endpoint=None: used.append(secret) or S3())
    assert asyncio.run(lakefs.health()) == layout.HEALTH_OK
    assert used == ["lakefs", "staging"]


def test_health_reports_the_failure_instead_of_raising(monkeypatch):
    class Down(S3):
        def list_buckets(self):
            raise ConnectionError("store down")

    use(monkeypatch, Down())
    assert asyncio.run(lakefs.health()) == f"{layout.HEALTH_ERROR}store down"


def test_a_repo_lakefs_refuses_raises_and_logs_its_reason(owned, monkeypatch, caplog):
    monkeypatch.setattr(lakefs, "_lakefs_api", lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(400, text="bad namespace")), base_url="http://lf.test/api/v1"))
    with caplog.at_level(logging.ERROR), pytest.raises(httpx.HTTPStatusError):
        asyncio.run(lakefs.create_dataset_repo(TENANT, DATASET))
    assert "bad namespace" in caplog.text


def test_a_preview_reads_only_the_first_rows_of_the_version_at_its_commit(owned, monkeypatch):
    key = f"{COMMIT}/{layout.INDEX_KEY}"  # bucket is the repo, key starts at the commit
    s3 = use(monkeypatch, S3({key: parquet_bytes(ROWS)}))
    rows = asyncio.run(lakefs.read_version_sample(TENANT, DATASET, VERSION, layout.INDEX_KEY, 2))
    assert rows == ROWS[:2]
    assert s3.ranges  # served by ranged GETs, not a whole-object download


def test_a_preview_of_an_empty_table_is_an_empty_list(owned, monkeypatch):
    key = f"{COMMIT}/{layout.INDEX_KEY}"  # bucket is the repo, key starts at the commit
    use(monkeypatch, S3({key: parquet_bytes([])}))
    assert asyncio.run(lakefs.read_version_sample(TENANT, DATASET, VERSION, layout.INDEX_KEY, 3)) == []


def test_a_whole_column_is_read_at_the_commit(owned, monkeypatch):
    key = f"{COMMIT}/{layout.INDEX_KEY}"  # bucket is the repo, key starts at the commit
    use(monkeypatch, S3({key: parquet_bytes(ROWS)}))
    assert asyncio.run(lakefs.read_version_column(TENANT, DATASET, VERSION, layout.INDEX_KEY, "path")) == [r["path"] for r in ROWS]


@pytest.mark.parametrize("read", [
    lambda: lakefs.read_version_column(TENANT, DATASET, VERSION, "../secret.parquet", "path"),
    lambda: lakefs.read_version_sample(TENANT, DATASET, VERSION, "/abs.parquet", 1),
    lambda: lakefs.read_staged_sample(TENANT, DATASET, IMPORT, "a/../../b", 1),
])
def test_a_key_that_escapes_the_version_is_refused_before_any_read(owned, monkeypatch, read):
    monkeypatch.setattr(lakefs, "_s3", lambda *a, **k: pytest.fail("store reached"))
    with pytest.raises(Problem) as err:
        asyncio.run(read())
    assert (err.value.status_code, err.value.code) == (400, lakefs.INVALID_PATH)


def test_the_review_preview_reads_the_staged_out_tree(owned, monkeypatch):
    key = f"{layout.staging_prefix(TENANT, DATASET, IMPORT)}{layout.OUT_PREFIX}{layout.INDEX_KEY}"
    use(monkeypatch, S3({key: parquet_bytes(ROWS)}))
    assert asyncio.run(lakefs.read_staged_sample(TENANT, DATASET, IMPORT, layout.INDEX_KEY, 1)) == ROWS[:1]


def test_the_object_view_seeks_and_stops_at_the_end(monkeypatch):
    s3 = S3({"k": b"0123456789"})
    obj = lakefs._S3Object(s3, "b", "k")
    assert obj.seek(-4, io.SEEK_END) == 6 and obj.read() == b"6789"
    assert obj.read() == b"" and obj.tell() == 10  # past the end: nothing more is fetched
    assert obj.seek(2) == 2 and obj.seek(3, io.SEEK_CUR) == 5 and obj.readable() and obj.seekable()


# --- the job's report ---------------------------------------------------------------------------------------------

def test_the_report_is_read_from_staging(owned, monkeypatch):
    key = layout.staging_prefix(TENANT, DATASET, IMPORT) + layout.REPORT_KEY
    use(monkeypatch, S3({key: b'{"outcome": "failed"}'}))
    report = asyncio.run(lakefs.read_report(TENANT, DATASET, IMPORT))
    assert report.outcome == Outcome.FAILED


def test_a_report_not_written_yet_is_none(owned, monkeypatch):
    use(monkeypatch, S3())
    assert asyncio.run(lakefs.read_report(TENANT, DATASET, IMPORT)) is None


def test_a_report_read_that_fails_otherwise_raises(owned, monkeypatch):
    class Denied(S3):
        def get_object(self, **kw):
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")

    use(monkeypatch, Denied())
    with pytest.raises(ClientError):
        asyncio.run(lakefs.read_report(TENANT, DATASET, IMPORT))


# --- staging listings, limits, purges -----------------------------------------------------------------------------

def test_staged_objects_are_listed_relative_to_their_prefix_across_pages(monkeypatch):
    s3 = use(monkeypatch, S3(pages=[{"Contents": [{"Key": "p/out/index.parquet", "Size": 3}]}, {}, {"Contents": [{"Key": "p/out/objects/a.jpg", "Size": 9}]}]))
    assert lakefs._list_staged("p/out/") == [("index.parquet", 3), ("objects/a.jpg", 9)]
    assert s3.listed == (settings.data_plane.staging_bucket, "p/out/")


@pytest.mark.parametrize("entries, fragment", [
    ([("index.parquet", 1), ("objects/a/../b", 1)], "clean relative path"),
    ([("index.parquet", 1), ("shell.sh", 1)], "unexpected output files"),
    ([("schema.json", 1)], "no index or data file"),
    ([("index.parquet", 1)] + [(f"objects/{i}", 1) for i in range(3)], "limit 2"),
    ([("index.parquet", 1), ("objects/a", 6), ("objects/b", 6)], "object bytes, limit 10"),
    ([("index.parquet", 1), ("objects/a", 8)], "an object is over 5 bytes"),
])
def test_staged_output_is_checked_not_believed(entries, fragment):
    limits = settings.dataset_limits.model_copy(update={"max_files": 2, "extracted_max_bytes": 10, "image_max_bytes": 5})
    if fragment == "limit 2":
        limits = limits.model_copy(update={"max_files": 2})
    assert fragment in lakefs._staged_violation(entries, limits)


def test_good_staged_output_has_no_violation():
    assert lakefs._staged_violation([("index.parquet", 1), ("objects/a", 1)], settings.dataset_limits) is None


def test_commit_refuses_output_over_the_byte_limit_as_422(owned, monkeypatch):
    monkeypatch.setattr(lakefs, "_lakefs_api", lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"results": []})), base_url="http://lf.test/api/v1"))
    monkeypatch.setattr(lakefs, "_list_staged", lambda prefix: [("index.parquet", 1), ("objects/a", 6), ("objects/b", 6)])
    limits = settings.dataset_limits.model_copy(update={"extracted_max_bytes": 10})
    with pytest.raises(Problem) as err:
        asyncio.run(lakefs.commit_staged(TENANT, DATASET, IMPORT, "m", limits))
    assert (err.value.status_code, err.value.code) == (422, lakefs.INVALID_STAGED_OUTPUT)


def test_a_purge_deletes_every_page_under_the_prefix_and_skips_empty_ones():
    s3 = S3(pages=[{"Contents": [{"Key": "a"}, {"Key": "b"}]}, {}, {"Contents": [{"Key": "c"}]}])
    lakefs._delete_prefix("bkt", "pre/", s3)
    assert s3.deleted == [("bkt", ["a", "b"]), ("bkt", ["c"])]


def test_purging_staging_removes_the_imports_prefix_only(owned, monkeypatch):
    s3 = use(monkeypatch, S3(pages=[{"Contents": [{"Key": "k1"}]}]))
    asyncio.run(lakefs.purge_staging(TENANT, DATASET, IMPORT))
    assert s3.listed == (settings.data_plane.staging_bucket, layout.staging_prefix(TENANT, DATASET, IMPORT))
    assert s3.deleted == [(settings.data_plane.staging_bucket, ["k1"])]
