"""/v1/admin/job-packages through the real router, database and data plane faked."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import problem
from app.core.enums import PackageStatus
from app.modules.admin.job_packages import routes
from tests.integrations.data_plane.test_job_package import package_row
from tests.support import FakeConn, TenantCtx

URL = "/v1/admin/job-packages"
SHA = "c" * 64


@pytest.fixture
def call(monkeypatch):
    def run(method, path, conn=None, **kw):
        async def get_pool():
            return SimpleNamespace(acquire=lambda: TenantCtx(conn))

        monkeypatch.setattr(routes, "get_pool", get_pool)
        app = FastAPI()
        problem.install(app)
        app.dependency_overrides[routes.get_platform_admin] = lambda: "ops"
        app.include_router(routes.router)
        return TestClient(app).request(method, path, **kw)

    return run


def test_publish_registers_the_bundled_source_as_the_actor(call, monkeypatch):
    async def publish(name, actor):
        return routes.JobPackage.model_validate(package_row(name, SHA, "jobs/x.zip", actor))

    monkeypatch.setattr(routes.data_plane, "publish_package", publish)
    res = call("POST", f"{URL}/ray_training/publish")
    assert res.status_code == 200 and (res.json()["status"], res.json()["published_by"]) == ("candidate", "ops")


def test_promote_sets_the_status_as_the_actor(call):
    conn = FakeConn(row=package_row("ray_jobs", SHA, "jobs/x.zip", "ops", PackageStatus.STABLE))
    res = call("PUT", f"{URL}/ray_jobs/{SHA}/status", conn, json={"status": "stable"})
    sql, args = conn.calls[0]
    assert res.status_code == 200 and sql.startswith("UPDATE job_packages") and args == ("ray_jobs", SHA, "stable", "ops")


def test_an_unknown_package_is_404_and_a_bad_hash_or_name_is_422(call):
    assert call("PUT", f"{URL}/ray_jobs/{SHA}/status", FakeConn(row=None), json={"status": "retired"}).status_code == 404
    assert call("PUT", f"{URL}/ray_jobs/nothex/status", FakeConn(), json={"status": "stable"}).status_code == 422
    assert call("GET", f"{URL}/other", FakeConn()).status_code == 422
