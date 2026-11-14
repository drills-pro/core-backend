"""/v1/admin/task-bundles and the auto-promote poller, through the real router, database and data plane faked."""
import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import problem
from app.core.enums import PackageAuditAction, PackageKind, PackageStatus, SmokeStatus
from app.core.job_packages import JobPackage
from app.modules.admin.task_bundles import routes, service
from app.modules.admin.task_bundles.constants import AUTO_PROMOTER, ProblemCode
from tests.integrations.data_plane.test_job_package import package_row
from tests.support import FakeConn, TenantCtx
from tests.support.bundles import SLUG, manifest
from tests.support.presets import preset_row
from tests.support.task_types import task_row

URL = "/v1/admin/task-bundles"
SHA = "c" * 64
NAME = f"task_{SLUG}"


def bundle_row(sha=SHA, **over):
    return package_row(NAME, sha, f"jobs/{SLUG}-{sha}.zip", "ci") | {"kind": PackageKind.TASK, "manifest": manifest()} | over


class Conn(FakeConn):
    """FakeConn with transactions, answering the promotion's queries with the rows they return."""

    def __init__(self, bundle=None, **kw):
        rows = {"FROM job_packages WHERE name": bundle, "UPDATE job_packages SET status": bundle, "INSERT INTO task_types": task_row(),
                "INSERT INTO task_presets": preset_row(), "task_bundle_settings": None, **kw.pop("rows", {})}
        super().__init__(rows=rows, **kw)

    def transaction(self):
        return TenantCtx(None)

    def sql(self):
        return [sql.split(" (")[0].split(" SET")[0].split(" WHERE")[0] for sql, _ in self.calls]


def _passed(**over):
    return bundle_row(smoke_status=SmokeStatus.PASSED, **over)


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


def test_promotion_flips_the_stable_bundle_and_rewrites_the_task_and_presets_in_one_go(call):
    conn = Conn(_passed(status=PackageStatus.STABLE))
    res = call("PUT", f"{URL}/{SLUG}/{SHA}/status", conn, json={"status": "stable"})
    assert res.status_code == 200 and res.json()["status"] == "stable"
    assert conn.sql()[1:] == [
        "UPDATE job_packages", "UPDATE job_packages", "INSERT INTO task_types", "DELETE FROM task_presets",
        *["INSERT INTO task_presets"] * 3, "INSERT INTO job_package_audit"]
    by_sql = {sql.split(" (")[0]: args for sql, args in conn.calls}
    assert by_sql["INSERT INTO task_types"] == (SLUG, "images", "imgcls.train:train", True, "image_encoder", "classify", SHA, "ops")
    assert by_sql["DELETE FROM task_presets WHERE task = $1 AND NOT"] == (SLUG, ["small", "medium", "large"])
    preset_args = by_sql["INSERT INTO task_presets"]  # the last one, large: its default values go in as they are
    assert (preset_args[1], preset_args[10]) == ("large", {"epochs": 30, "batch_size": 16, "image_size": 224})
    assert by_sql["INSERT INTO job_package_audit"] == (NAME, SHA, PackageAuditAction.PROMOTE, "ops")


def test_a_bundle_whose_smoke_has_not_passed_is_not_promoted(call):
    conn = Conn(bundle_row())
    res = call("PUT", f"{URL}/{SLUG}/{SHA}/status", conn, json={"status": "stable"})
    assert res.status_code == 409 and res.json()["code"] == ProblemCode.SMOKE_NOT_PASSED and len(conn.calls) == 1


def test_retiring_a_candidate_is_audited_and_the_stable_one_is_refused(call):
    conn = Conn(bundle_row())
    assert call("PUT", f"{URL}/{SLUG}/{SHA}/status", conn, json={"status": "retired"}).status_code == 200
    assert conn.calls[-1][1] == (NAME, SHA, PackageAuditAction.RETIRE, "ops")
    res = call("PUT", f"{URL}/{SLUG}/{SHA}/status", Conn(_passed(status=PackageStatus.STABLE)), json={"status": "retired"})
    assert res.status_code == 409 and res.json()["code"] == ProblemCode.BUNDLE_IS_STABLE


def test_an_unknown_bundle_is_404_and_candidate_is_not_a_target(call):
    assert call("PUT", f"{URL}/{SLUG}/{SHA}/status", Conn(None), json={"status": "stable"}).status_code == 404
    assert call("PUT", f"{URL}/{SLUG}/{SHA}/status", Conn(bundle_row()), json={"status": "candidate"}).status_code == 422
    assert call("PUT", f"{URL}/not-a-task/{SHA}/status", Conn(bundle_row()), json={"status": "stable"}).status_code == 422


def test_settings_default_to_auto_promote_and_can_be_switched_off(call):
    assert call("GET", f"{URL}/{SLUG}/settings", Conn()).json() == {"slug": SLUG, "auto_promote": True, "updated_by": None, "updated_at": None}
    stored = {"slug": SLUG, "auto_promote": False, "updated_by": "ops", "updated_at": datetime.now(UTC)}
    res = call("PUT", f"{URL}/{SLUG}/settings", Conn(rows={"task_bundle_settings": stored}), json={"auto_promote": False})
    assert res.status_code == 200 and res.json()["auto_promote"] is False


@pytest.fixture
def poller(monkeypatch):
    def run(conn, enabled=True):
        async def get_pool():
            return SimpleNamespace(acquire=lambda: TenantCtx(conn))

        async def unchanged(package):
            return package

        monkeypatch.setattr(service, "get_pool", get_pool)
        monkeypatch.setattr(service.data_plane, "refresh_package_smoke", unchanged)
        monkeypatch.setattr(service.settings.training, "bundle_auto_promote", enabled)
        asyncio.run(service.auto_promote())
        return [args for sql, args in conn.calls if sql.startswith("INSERT INTO job_package_audit")]

    return run


def test_the_poller_promotes_candidates_whose_smoke_passed(poller):
    conn = Conn(_passed(), rowset=[_passed(), bundle_row(sha="d" * 64)])
    assert poller(conn) == [(NAME, SHA, PackageAuditAction.PROMOTE, AUTO_PROMOTER)]


def test_the_poller_leaves_a_slug_that_opted_out_and_does_nothing_when_switched_off(poller):
    opted_out = {"slug": SLUG, "auto_promote": False, "updated_by": "ops", "updated_at": datetime.now(UTC)}
    assert poller(Conn(_passed(), rowset=[_passed()], rows={"task_bundle_settings": opted_out})) == []
    conn = Conn(_passed(), rowset=[_passed()])
    assert poller(conn, enabled=False) == [] and conn.calls == []
