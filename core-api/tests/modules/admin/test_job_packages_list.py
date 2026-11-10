"""GET /v1/admin/job-packages/{name}: the package rows of one name, each with its smoke settled, behind the admin gate."""
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.core.enums import JobPackageName, PackageStatus, SmokeStatus
from app.modules.admin.job_packages import routes
from tests.modules.admin.conftest import ADMIN_HEADERS, AdminConn
from tests.support import TenantCtx, make_client

URL = "/v1/admin/job-packages"


def package(sha, status=PackageStatus.CANDIDATE):
    return dict(name=JobPackageName.RAY_JOBS, sha256=sha * 64, s3_key=f"jobs/{sha}.zip", status=status, published_by="ci", updated_by="ci",
                updated_at=datetime(2026, 1, 1, tzinfo=UTC), smoke_status=SmokeStatus.PENDING, smoke_job_id="smoke-1")


@pytest.fixture
def api(monkeypatch, dev_headers):
    def build(conn, headers=ADMIN_HEADERS):
        async def get_pool():
            return SimpleNamespace(acquire=lambda: TenantCtx(conn))

        async def settle(pkg):
            return pkg.model_copy(update={"smoke_status": SmokeStatus.PASSED})

        monkeypatch.setattr(routes, "get_pool", get_pool)
        monkeypatch.setattr(routes.data_plane, "refresh_package_smoke", settle)
        return make_client(routes.router, headers=headers)

    return build


def test_every_row_of_the_name_is_returned_with_its_smoke_refreshed(api):
    conn = AdminConn(rowset=[package("a", PackageStatus.STABLE), package("b")])
    res = api(conn).get(f"{URL}/ray_jobs")
    assert res.status_code == 200
    assert [(p["sha256"][0], p["status"], p["smoke_status"]) for p in res.json()] == [("a", "stable", "passed"), ("b", "candidate", "passed")]
    assert conn.calls[0][1] == (JobPackageName.RAY_JOBS,)


def test_a_caller_without_credentials_is_401_and_the_database_is_not_asked(api):
    conn = AdminConn(rowset=[package("a")])
    res = api(conn, headers={}).get(f"{URL}/ray_jobs")
    assert (res.status_code, res.json()["code"]) == (401, "unauthenticated") and conn.calls == []
