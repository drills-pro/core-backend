"""job_package: upload and read-back failures, the smoke job's submission and its settling when Ray cannot answer."""
import asyncio
import hashlib
import io
import json
import re
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from botocore.exceptions import ClientError

from app.core.enums import JobPackageName, PackageStatus, SmokeStatus
from app.core.problem import Problem
from app.integrations.data_plane import job_catalog, job_package, layout
from tests.support import TenantCtx

SHA = "d" * 64


def client_error(code):
    return ClientError({"Error": {"Code": code}}, "Op")


def package(smoke_job_id=None, smoke_status=SmokeStatus.PENDING):
    return job_package.JobPackage(
        name=JobPackageName.RAY_JOBS, sha256=SHA, s3_key="jobs/x.zip", status=PackageStatus.CANDIDATE, published_by="ci",
        updated_by="ci", updated_at=datetime.now(UTC), smoke_status=smoke_status, smoke_job_id=smoke_job_id)


class S3:
    def __init__(self, head=None, get=None, objects=None):
        self.head, self.get, self.objects, self.put = head, get, objects or {}, []

    def head_object(self, Bucket, Key):
        if self.head:
            raise self.head
        return {"ContentLength": len(self.objects[Key])}

    def get_object(self, Bucket, Key):
        if self.get:
            raise self.get
        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object(self, **kw):
        self.put.append(kw["Key"])


@pytest.fixture
def s3(monkeypatch):
    holder = SimpleNamespace(client=S3())
    monkeypatch.setattr(job_package.lakefs, "_s3", lambda secret, endpoint=None: holder.client)
    monkeypatch.setattr(job_package, "_uploaded", set())
    return holder


def test_an_unexpected_head_error_is_raised_and_nothing_is_uploaded(s3):
    s3.client = S3(head=client_error("403"))
    with pytest.raises(ClientError):
        job_package._upload_once(b"zip", "jobs/k.zip")
    assert s3.client.put == [] and "jobs/k.zip" not in job_package._uploaded


def test_an_upload_over_the_size_cap_is_413(monkeypatch):
    monkeypatch.setattr(job_package, "MAX_PACKAGE_BYTES", 4)
    with pytest.raises(Problem) as err:
        job_package._check_upload(JobPackageName.RAY_JOBS, b"12345", hashlib.sha256(b"12345").hexdigest())
    assert (err.value.status_code, err.value.code) == (413, job_package.INVALID_PACKAGE)


def test_an_upload_that_is_not_a_zip_is_422():
    data = b"definitely not a zip"
    with pytest.raises(Problem) as err:
        job_package._check_upload(JobPackageName.RAY_JOBS, data, hashlib.sha256(data).hexdigest())
    assert (err.value.status_code, err.value.detail) == (422, "the body is not a zip archive")


def test_a_staged_object_over_the_size_cap_is_422_and_never_downloaded(s3, monkeypatch):
    monkeypatch.setattr(job_package, "MAX_PACKAGE_BYTES", 2)
    s3.client = S3(objects={"k": b"abc"}, get=AssertionError("downloaded"))
    with pytest.raises(Problem) as err:
        job_package._read_staged("k")
    assert err.value.status_code == 422


def test_a_staged_read_with_another_s3_error_is_not_masked_as_a_422(s3):
    s3.client = S3(head=client_error("AccessDenied"))
    with pytest.raises(ClientError):
        job_package._read_staged("k")


def test_smoke_results_are_read_from_the_package_prefix(s3):
    s3.client = S3(objects={"k": b'{"ok": true}'})
    assert job_package._read_smoke("k") == b'{"ok": true}'
    s3.client = S3(get=client_error("NoSuchKey"))
    assert job_package._read_smoke("k") is None
    s3.client = S3(get=client_error("AccessDenied"))
    with pytest.raises(ClientError):
        job_package._read_smoke("k")


@pytest.fixture
def db(monkeypatch):
    calls = []

    async def get_pool():
        return SimpleNamespace(acquire=lambda: TenantCtx(None))

    async def start_smoke(conn, name, sha, job_id):
        calls.append(("start", name, sha, job_id))

    async def get(conn, name, sha):
        return package(smoke_job_id=calls[-1][3])

    async def set_smoke(conn, name, sha, smoke):
        calls.append(("set", smoke))
        return package(smoke_job_id="j", smoke_status=smoke)

    monkeypatch.setattr(job_package, "get_pool", get_pool)
    monkeypatch.setattr(job_package.job_packages, "start_smoke", start_smoke)
    monkeypatch.setattr(job_package.job_packages, "get", get)
    monkeypatch.setattr(job_package.job_packages, "set_smoke", set_smoke)
    return calls


def test_the_smoke_job_is_submitted_under_a_stable_id_and_recorded(db, monkeypatch):
    sent = {}

    async def submit(**kw):
        sent.update(kw)
        return "ray-id"

    async def url(name, sha256):
        return f"http://ray/{name}/{sha256}"

    monkeypatch.setattr(job_package.job_submit, "submit", submit)
    monkeypatch.setattr(job_package, "presigned_url", url)
    result = asyncio.run(job_package.start_smoke(package()))
    job_id = f"smoke-ray-jobs-{SHA[:12]}"
    assert result.smoke_job_id == job_id and db == [("start", JobPackageName.RAY_JOBS, SHA, job_id)]
    assert (sent["job_id"], sent["module"], sent["package_url"]) == (job_id, job_catalog.SMOKE_ENTRYPOINTS[JobPackageName.RAY_JOBS], f"http://ray/ray_jobs/{SHA}")
    assert json.loads(sent["spec_json"])["prefix"] == layout.smoke_prefix(JobPackageName.RAY_JOBS, SHA)
    assert sent["timeout_seconds"] == job_package.SMOKE_TIMEOUT_SECONDS


def test_a_package_that_already_has_a_smoke_job_is_not_submitted_again(db, monkeypatch):
    monkeypatch.setattr(job_package.job_submit, "submit", lambda **kw: pytest.fail("submitted twice"))
    started = package(smoke_job_id="smoke-1")
    assert asyncio.run(job_package.start_smoke(started)) is started and db == []


def test_a_retried_smoke_gets_a_new_job_id_and_result_prefix(db, monkeypatch):
    sent = {}

    async def submit(**kw):
        sent.update(kw)

    async def reset(conn, name, sha):
        db.append(("reset", name, sha))
        return package()

    async def url(name, sha256):
        return "http://ray/x"

    monkeypatch.setattr(job_package.job_submit, "submit", submit)
    monkeypatch.setattr(job_package, "presigned_url", url)
    monkeypatch.setattr(job_package.job_packages, "reset_failed_smoke", reset)
    result = asyncio.run(job_package.retry_smoke(JobPackageName.RAY_JOBS, SHA))
    attempt = sent["job_id"].removeprefix(f"smoke-ray-jobs-{SHA[:12]}-")
    assert re.fullmatch(r"\d{14}", attempt) and result.smoke_job_id == sent["job_id"]
    assert json.loads(sent["spec_json"])["prefix"] == layout.smoke_prefix(JobPackageName.RAY_JOBS, SHA, attempt) != layout.smoke_prefix(JobPackageName.RAY_JOBS, SHA)
    assert job_package._smoke_attempt(package(smoke_job_id=sent["job_id"])) == attempt and job_package._smoke_attempt(package(smoke_job_id="smoke-ray-jobs-" + SHA[:12])) == ""


def test_a_retry_that_lost_the_compare_and_set_submits_nothing(db, monkeypatch):
    async def reset(conn, name, sha):
        return None

    monkeypatch.setattr(job_package.job_submit, "submit", lambda **kw: pytest.fail("submitted"))
    monkeypatch.setattr(job_package.job_packages, "reset_failed_smoke", reset)
    assert asyncio.run(job_package.retry_smoke(JobPackageName.RAY_JOBS, SHA)) is None


def test_a_retry_ray_refuses_leaves_the_package_failed(db, monkeypatch):
    async def reset(conn, name, sha):
        return package()

    async def submit(**kw):
        raise httpx.ConnectError("ray down")

    async def url(name, sha256):
        return "http://ray/x"

    monkeypatch.setattr(job_package.job_submit, "submit", submit)
    monkeypatch.setattr(job_package, "presigned_url", url)
    monkeypatch.setattr(job_package.job_packages, "reset_failed_smoke", reset)
    with pytest.raises(httpx.ConnectError):
        asyncio.run(job_package.retry_smoke(JobPackageName.RAY_JOBS, SHA))
    assert db == [("set", SmokeStatus.FAILED)]


@pytest.mark.parametrize("pkg", [package(smoke_job_id="j", smoke_status=SmokeStatus.PASSED), package(smoke_job_id=None)])
def test_a_settled_or_never_started_smoke_is_returned_untouched(db, monkeypatch, pkg):
    monkeypatch.setattr(job_package, "_read_smoke", lambda key: pytest.fail("read the result"))
    assert asyncio.run(job_package.refresh_smoke(pkg)) is pkg and db == []


def test_a_job_ray_cannot_answer_for_stays_pending(db, monkeypatch):
    async def status(job_id):
        raise httpx.ConnectError("ray down")

    monkeypatch.setattr(job_package, "_read_smoke", lambda key: None)
    monkeypatch.setattr(job_package.compute, "status", status)
    pending = package(smoke_job_id="j")
    assert asyncio.run(job_package.refresh_smoke(pending)) is pending and db == []
