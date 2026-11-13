"""/v1/admin/task-bundles through the real router, database and data plane faked."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import problem
from app.core.enums import PackageKind
from app.core.job_packages import JobPackage
from app.modules.admin.task_bundles import routes, service
from tests.integrations.data_plane.test_job_package import package_row
from tests.support import TenantCtx
from tests.support.bundles import SLUG, manifest

URL = "/v1/admin/task-bundles"
SHA = "c" * 64
NAME = f"task_{SLUG}"


def bundle_row(sha=SHA, **over):
    return package_row(NAME, sha, f"jobs/{SLUG}-{sha}.zip", "ci") | {"kind": PackageKind.TASK, "manifest": manifest()} | over


@pytest.fixture
def call(monkeypatch):
    def run(method, path, conn=None, **kw):
        async def get_pool():
            return SimpleNamespace(acquire=lambda: TenantCtx(conn))

        async def unchanged(package):
            return package

        monkeypatch.setattr(service, "get_pool", get_pool, raising=False)
        monkeypatch.setattr(service.data_plane, "start_package_smoke", unchanged)
        monkeypatch.setattr(service.data_plane, "refresh_package_smoke", unchanged)
        app = FastAPI()
        problem.install(app)
        app.dependency_overrides[routes.get_platform_admin] = lambda: "ops"
        app.include_router(routes.router)
        return TestClient(app).request(method, path, **kw)

    return run


def test_register_hands_the_key_to_the_data_plane_and_starts_the_smoke(call, monkeypatch):
    seen = []

    async def register(s3_key, actor):
        seen.append((s3_key, actor))
        return JobPackage.model_validate(bundle_row())

    monkeypatch.setattr(service.data_plane, "register_staged_bundle", register)
    res = call("POST", f"{URL}/register", json={"s3_key": f"jobs/{SLUG}-{SHA}.zip"})
    body = res.json()
    assert res.status_code == 200 and seen == [(f"jobs/{SLUG}-{SHA}.zip", "ops")]
    assert (body["kind"], body["name"], body["manifest"]["slug"], body["status"]) == ("task", NAME, SLUG, "candidate")
    assert call("POST", f"{URL}/register", json={"s3_key": "k", "extra": 1}).status_code == 422
