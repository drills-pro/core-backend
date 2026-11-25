"""/v1/admin/job-packages through the real router, database and data plane faked."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import problem
from app.core.enums import PackageAuditAction, PackageStatus, SmokeStatus
from app.integrations import data_plane
from app.integrations.data_plane import job_package
from app.modules.admin.job_packages import routes, service
from tests.support import FakeConn, TenantCtx
from tests.support.packages import package_row

URL = "/v1/admin/job-packages"
SHA = "c" * 64


@pytest.fixture
def call(monkeypatch):
    def run(method, path, conn=None, **kw):
        async def get_pool():
            return SimpleNamespace(acquire=lambda: TenantCtx(conn))

        async def unchanged(package):
            return package

        monkeypatch.setattr(service, "get_pool", get_pool)
        monkeypatch.setattr(service.data_plane, "start_package_smoke", unchanged)
        monkeypatch.setattr(service.data_plane, "refresh_package_smoke", unchanged)
        monkeypatch.setattr(job_package, "refresh_smoke", unchanged)  # the gates of a retry settle the smoke through it
        app = FastAPI()
        problem.install(app)
        app.dependency_overrides[routes.get_platform_admin] = lambda: "ops"
        app.include_router(routes.router)
        return TestClient(app).request(method, path, **kw)

    return run


def test_publish_registers_the_bundled_source_as_the_actor(call, monkeypatch):
    async def publish(name, actor):
        return routes.JobPackage.model_validate(package_row(name, SHA, "jobs/x.zip", actor))

    monkeypatch.setattr(service.data_plane, "publish_package", publish)
    res = call("POST", f"{URL}/ray_training/publish")
    assert res.status_code == 200 and (res.json()["status"], res.json()["published_by"]) == ("candidate", "ops")


def test_the_status_write_moved_to_the_target_call(call):
    assert call("PUT", f"{URL}/ray_jobs/{SHA}/status", FakeConn(), json={"status": "stable"}).status_code == 404


def test_a_bad_name_or_hash_is_422(call):
    assert call("POST", f"{URL}/other/target", FakeConn(), json={"s3_key": "k"}).status_code == 422
    assert call("POST", f"{URL}/ray_jobs/nothex/smoke", FakeConn()).status_code == 422


def test_target_registers_a_new_version_as_the_actor_and_leaves_its_smoke_running(call, monkeypatch):
    seen = []

    async def register(name, s3_key, actor):
        seen.append((name, s3_key, actor))
        return routes.JobPackage.model_validate(package_row(name, SHA, s3_key, actor))

    monkeypatch.setattr(service.data_plane, "register_staged_package", register)
    conn = FakeConn()
    res = call("POST", f"{URL}/ray_jobs/target", conn, json={"s3_key": "jobs/ray-jobs-x.zip"})
    assert res.status_code == 200 and seen == [("ray_jobs", "jobs/ray-jobs-x.zip", "ops")]
    assert (res.json()["status"], res.json()["smoke_status"]) == ("candidate", "pending")
    assert not any(c[0].startswith("UPDATE job_packages SET status") for c in conn.calls)  # no move: the poller does it
    assert call("POST", f"{URL}/ray_jobs/target", FakeConn(), json={"s3_key": "k", "extra": 1}).status_code == 422


def test_target_of_a_version_whose_smoke_passed_moves_it_now_and_audits_the_actor(call, monkeypatch):
    async def register(name, s3_key, actor):
        return routes.JobPackage.model_validate(package_row(name, SHA, s3_key, actor) | {"smoke_status": SmokeStatus.PASSED})

    monkeypatch.setattr(service.data_plane, "register_staged_package", register)
    conn = FakeConn(row=package_row("ray_jobs", SHA, "jobs/x.zip", "ops", PackageStatus.RETIRED) | {"smoke_status": SmokeStatus.PASSED})
    res = call("POST", f"{URL}/ray_jobs/target", conn, json={"s3_key": "jobs/ray-jobs-x.zip"})
    assert res.status_code == 200
    assert conn.calls[-1][1] == ("ray_jobs", SHA, PackageAuditAction.PROMOTE, "ops")  # the rollback: one transaction with its audit row


def test_target_of_a_version_whose_smoke_failed_smokes_it_again(call, monkeypatch):
    failed = package_row("ray_jobs", SHA, "jobs/x.zip", "ops") | {"smoke_status": SmokeStatus.FAILED, "smoke_job_id": "smoke-1"}
    again = {**failed, "smoke_status": SmokeStatus.PENDING, "smoke_job_id": "smoke-2"}

    async def register(name, s3_key, actor):
        return routes.JobPackage.model_validate(failed)

    async def retry(name, sha256):
        return routes.JobPackage.model_validate(again)

    monkeypatch.setattr(service.data_plane, "register_staged_package", register)
    monkeypatch.setattr(job_package, "retry_smoke", retry)
    res = call("POST", f"{URL}/ray_jobs/target", FakeConn(row=failed), json={"s3_key": "jobs/ray-jobs-x.zip"})
    assert res.status_code == 200 and res.json()["smoke_job_id"] == "smoke-2"


def test_retrying_an_unknown_package_is_404(call, monkeypatch):
    async def retry(name, sha256):
        return None

    monkeypatch.setattr(job_package, "retry_smoke", retry)
    assert call("POST", f"{URL}/ray_jobs/{SHA}/smoke", FakeConn(row=None)).status_code == 404


def _failed(status=PackageStatus.CANDIDATE, smoke=SmokeStatus.FAILED):
    return package_row("ray_jobs", SHA, "jobs/x.zip", "ops", status) | {"smoke_status": smoke, "smoke_job_id": "smoke-1"}


def test_a_failed_candidate_is_smoked_again(call, monkeypatch):
    again = routes.JobPackage.model_validate(_failed(smoke=SmokeStatus.PENDING) | {"smoke_job_id": "smoke-2"})
    seen = []

    async def retry(name, sha256):
        seen.append((name, sha256))
        return again

    monkeypatch.setattr(job_package, "retry_smoke", retry)
    res = call("POST", f"{URL}/ray_jobs/{SHA}/smoke", FakeConn(row=_failed()))
    assert res.status_code == 200 and seen == [("ray_jobs", SHA)] and res.json()["smoke_status"] == "pending" and res.json()["smoke_job_id"] == "smoke-2"


@pytest.mark.parametrize("row", [
    _failed(PackageStatus.STABLE), _failed(PackageStatus.RETIRED), _failed(smoke=SmokeStatus.PENDING), _failed(smoke=SmokeStatus.PASSED)])
def test_only_a_failed_candidate_can_be_retried(call, monkeypatch, row):
    async def retry(name, sha256):
        raise AssertionError("not retryable: nothing may be submitted")

    monkeypatch.setattr(job_package, "retry_smoke", retry)
    res = call("POST", f"{URL}/ray_jobs/{SHA}/smoke", FakeConn(row=row))
    assert res.status_code == 409 and res.json()["code"] == data_plane.SmokeProblem.NOT_RETRYABLE
