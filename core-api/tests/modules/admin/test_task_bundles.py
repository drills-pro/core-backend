"""/v1/admin/task-bundles: the reads, the switches, and what a bundle writes when it becomes its task's target, through the real
router and a faked database. A bundle is registered and moved through the job-packages target call (test_job_packages.py)."""
import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import problem
from app.core.enums import PackageAuditAction, PackageKind, PackageStatus, SmokeStatus, TaskType
from app.core.job_packages import JobPackage
from app.integrations.data_plane import job_package
from app.modules.admin.task_bundles import routes, service
from tests.support import FakeConn, TenantCtx
from tests.support.bundles import SLUG, manifest
from tests.support.packages import package_row
from tests.support.presets import preset_row
from tests.support.task_types import task_row

URL = "/v1/admin/task-bundles"
SHA = "c" * 64
NAME = f"task_{SLUG}"


def bundle_row(sha=SHA, **over):
    return package_row(NAME, sha, f"jobs/{SLUG}-{sha}.zip", "ci") | {"kind": PackageKind.TASK, "manifest": manifest()} | over


class Conn(FakeConn):
    """FakeConn with transactions, answering the promotion's queries with the rows they return."""

    def __init__(self, bundle=None, fetches=None, **kw):
        self.fetches = fetches  # fetch() answers by SQL substring when given
        rows = {"FROM job_packages WHERE name": bundle, "UPDATE job_packages SET status": bundle, "INSERT INTO task_types": task_row(),
                "INSERT INTO task_presets": preset_row(), "task_bundle_settings": None, **kw.pop("rows", {})}
        super().__init__(rows=rows, **kw)

    def transaction(self):
        return TenantCtx(None)

    async def fetch(self, sql, *args):
        if self.fetches is None:
            return await super().fetch(sql, *args)
        self.calls.append((sql, args))
        return next(v for key, v in self.fetches.items() if key in sql)

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
        monkeypatch.setattr(service.data_plane, "refresh_package_smoke", unchanged)
        monkeypatch.setattr(job_package, "refresh_smoke", unchanged)
        app = FastAPI()
        problem.install(app)
        app.dependency_overrides[routes.get_platform_admin] = lambda: "ops"
        app.include_router(routes.router)
        return TestClient(app).request(method, path, **kw)

    return run


def test_a_manual_bundle_move_takes_only_stable_or_retired(call):
    assert call("POST", f"{URL}/register", json={"s3_key": "k"}).status_code == 405  # the path exists for GET /{slug}
    assert call("PUT", f"{URL}/{SLUG}/{SHA}/status", Conn(_passed()), json={"status": "candidate"}).status_code == 422


def test_promotion_writes_the_task_row_and_presets_with_the_package_move_in_one_go(monkeypatch):
    conn = Conn(_passed(status=PackageStatus.STABLE))

    async def get_pool():
        return SimpleNamespace(acquire=lambda: TenantCtx(conn))

    monkeypatch.setattr(service, "get_pool", get_pool)
    promoted = asyncio.run(service.promote(JobPackage.model_validate(_passed()), "ops"))
    assert promoted is not None
    by_sql = {sql.split(" (")[0]: args for sql, args in conn.calls}
    assert by_sql["INSERT INTO task_types"] == (SLUG, "images", "imgcls.train:train", True, "image_encoder", "classify", SHA, "ops")
    assert by_sql["DELETE FROM task_presets WHERE task = $1 AND NOT"] == (SLUG, ["small", "medium", "large"])
    preset_args = by_sql["INSERT INTO task_presets"]  # the last one, large: its default values go in as they are
    assert (preset_args[1], preset_args[10]) == ("large", {"epochs": 30, "batch_size": 16, "image_size": 224})
    assert by_sql["INSERT INTO job_package_audit"] == (NAME, SHA, PackageAuditAction.PROMOTE, "ops")


def test_an_unknown_bundle_is_404_and_a_bad_slug_is_422(call):
    assert call("GET", f"{URL}/{SLUG}/{SHA}", Conn(None)).status_code == 404
    assert call("GET", f"{URL}/not-a-task/{SHA}", Conn()).status_code == 422


def test_settings_default_to_auto_promote_and_can_be_switched_off(call):
    assert call("GET", f"{URL}/{SLUG}/settings", Conn()).json() == {"slug": SLUG, "auto_promote": True, "updated_by": None, "updated_at": None}
    stored = {"slug": SLUG, "auto_promote": False, "updated_by": "ops", "updated_at": datetime.now(UTC)}
    res = call("PUT", f"{URL}/{SLUG}/settings", Conn(rows={"task_bundle_settings": stored}), json={"auto_promote": False})
    assert res.status_code == 200 and res.json()["auto_promote"] is False


def test_bundles_are_listed_and_read_with_their_slug_and_smoke_status(call):
    rows = [bundle_row(), package_row("task_object-detection", "d" * 64, "k", "ci") | {"kind": "task", "manifest": manifest(slug="object-detection")}]
    every = call("GET", URL, Conn(rowset=rows)).json()
    assert [(b["slug"], b["kind"], b["smoke_status"]) for b in every] == [(SLUG, "task", "pending"), ("object-detection", "task", "pending")]
    assert [b["sha256"] for b in call("GET", f"{URL}/{SLUG}", Conn(rowset=rows)).json()] == [SHA]
    one = call("GET", f"{URL}/{SLUG}/{SHA}", Conn(_passed()))
    assert one.status_code == 200 and (one.json()["slug"], one.json()["smoke_status"], one.json()["manifest"]["trainer"]) == (
        SLUG, "passed", "imgcls.train:train")


def test_the_catalog_shows_each_task_with_its_presets_and_backing_bundle(call):
    stable = _passed(status=PackageStatus.STABLE)
    conn = Conn(fetches={"FROM task_types": [task_row() | {"bundle_sha256": SHA}, task_row(TaskType.TEXT_CLASSIFICATION)],
                         "FROM job_packages": [stable], "FROM task_presets": [preset_row()]})
    res = call("GET", f"{URL}/catalog", conn).json()
    assert [(t["task"]["slug"], t["bundle"] and t["bundle"]["sha256"], len(t["presets"]), t["auto_promote"]) for t in res] == [
        (SLUG, SHA, 1, True), ("text-classification", None, 1, True)]


def test_a_task_and_a_preset_can_be_switched_off(call):
    conn = Conn(rows={"UPDATE task_types SET enabled": task_row() | {"enabled": False}, "UPDATE task_presets SET enabled": preset_row(enabled=False)})
    assert call("PUT", f"{URL}/catalog/{SLUG}/enabled", conn, json={"enabled": False}).json()["enabled"] is False
    assert call("PUT", f"{URL}/catalog/{SLUG}/presets/small/enabled", conn, json={"enabled": False}).json()["enabled"] is False
    missing = Conn(rows={"UPDATE task_types SET enabled": None, "UPDATE task_presets SET enabled": None})
    assert call("PUT", f"{URL}/catalog/{SLUG}/enabled", missing, json={"enabled": True}).status_code == 404
    assert call("PUT", f"{URL}/catalog/{SLUG}/presets/small/enabled", missing, json={"enabled": True}).status_code == 404


def test_only_a_failed_candidate_bundle_is_smoked_again(call, monkeypatch):
    async def retry(name, sha):
        return JobPackage.model_validate(bundle_row(smoke_job_id="smoke-2"))

    monkeypatch.setattr(job_package, "retry_smoke", retry)
    res = call("POST", f"{URL}/{SLUG}/{SHA}/smoke", Conn(bundle_row(smoke_status=SmokeStatus.FAILED)))
    assert res.status_code == 200 and res.json()["smoke_job_id"] == "smoke-2"
    res = call("POST", f"{URL}/{SLUG}/{SHA}/smoke", Conn(_passed()))
    assert res.status_code == 409 and res.json()["code"] == job_package.SmokeProblem.NOT_RETRYABLE
