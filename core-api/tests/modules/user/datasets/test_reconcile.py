"""Reconcilers and sweeps with a fake clock: Ray polling, import expiry, purge of deleted datasets.

The database is an in-memory fake that answers the reconcile module's own SQL constants; Ray and the gateway
are fakes behind `reconcile.ray_client` and `reconcile.data_plane`.
"""
import asyncio
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import background
from app.core.settings import settings
from app.integrations.data_plane.report import ImportReport, IssueCode, Outcome, Stats
from app.modules.user.datasets import import_service as svc
from app.modules.user.datasets import reconcile as rec
from app.modules.user.datasets.constants import (
    DATASET_RETENTION_DAYS,
    IMPORT_REVIEW_TTL,
    SYSTEM_ACTOR,
    AuditAction,
    ImportStatus,
    RayJobStatus,
)
from tests.support import PROJECT, TENANT

S, R = ImportStatus, RayJobStatus
T0 = datetime(2026, 1, 1, tzinfo=UTC)
OK = ImportReport(outcome=Outcome.OK, stats=Stats(file_count=1, total_bytes=7, row_count=1))
BAD = ImportReport.model_validate({"outcome": "failed", "issues": [{"code": "corrupt_image", "severity": "error", "message": "x"}]})


class FakeDb:
    def __init__(self):
        self.imports, self.datasets, self.audits, self.purged_rows = {}, {}, [], []

    def seed(self, status, **kw):
        row = {"id": uuid.uuid4(), "tenant_id": uuid.UUID(TENANT), "project_id": PROJECT, "dataset_id": uuid.uuid4(),
               "status": status, "report": None, "ray_job_id": "job", "expires_at": None, "created_at": T0, "completed_at": None, **kw}
        self.imports[row["id"]] = row
        return row

    def dataset(self, deleted_at):
        ds = uuid.uuid4()
        self.datasets[ds] = deleted_at
        return ds

    def __call__(self, tenant_id):
        return _Conn(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def fetch_cross(self, sql, *a):
        if sql == rec._RUNNING_TENANTS:
            return [(i["tenant_id"],) for i in self.imports.values() if i["status"] in rec._RUNNING_STATUSES][:1]
        if sql == rec._EXPIRED_TENANTS:
            return [(i["tenant_id"],) for i in self.imports.values() if self._due(i, a[0])][:1]
        if sql == rec._PURGEABLE:
            return [(uuid.UUID(TENANT), d) for d, at in self.datasets.items() if at and at < a[0]]
        raise AssertionError(sql)

    @staticmethod
    def _due(i, now):
        return i["status"] in rec._EXPIRABLE_STATUSES and i["expires_at"] and i["expires_at"] < now


class _Conn:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def fetch(self, sql, *a):
        db = self.db
        if sql == rec._RUNNING:
            return [dict(i) for i in db.imports.values() if i["status"] in a[1]]
        if sql == rec._DATASET_IMPORTS:
            return [{"id": i["id"]} for i in db.imports.values() if i["dataset_id"] == a[0]]
        if sql == rec._EXPIRED:
            return [dict(i) for i in db.imports.values() if i["status"] in a[1] and db._due(i, a[2])]
        raise AssertionError(sql)

    async def fetchrow(self, sql, *a):
        assert sql == svc._TRANSITION
        row = self.db.imports[a[0]]
        if row["status"] != a[1]:
            return None
        row.update(status=a[2], expires_at=a[3], completed_at=a[4])
        return dict(row)

    async def fetchval(self, sql, *a):
        db = self.db
        if sql == rec._LOCK_PURGEABLE:
            at = db.datasets.get(a[0])
            return 1 if at and at < a[1] else None
        assert sql == rec._PURGE_ROWS
        del db.datasets[a[0]]
        db.purged_rows.append(a[0])

    async def execute(self, sql, *a):
        if sql == rec._STORE_REPORT:
            self.db.imports[a[0]]["report"] = a[1]
        else:  # audit.record: tenant, project, actor, action, entity_type, entity_id
            self.db.audits.append((a[2], a[3], a[5]))


class Gateway:
    def __init__(self, reports=None):
        self.reports, self.purged_staging, self.repos = reports or {}, [], set()
        self.deleted_repos = []

    async def read_report(self, tenant_id, dataset_id, import_id):
        return self.reports.get(import_id)

    async def purge_staging(self, tenant_id, dataset_id, import_id):
        self.purged_staging.append(import_id)

    async def delete_dataset_repo(self, tenant_id, dataset_id):
        self.repos.discard(dataset_id)  # a repo that is already gone is not an error
        self.deleted_repos.append(dataset_id)


@pytest.fixture
def env(monkeypatch):
    db, gw, ray = FakeDb(), Gateway(), SimpleNamespace(status=R.PENDING, stopped=[])

    async def get_status(job_id):
        return ray.status

    async def stop_job(job_id):
        ray.stopped.append(job_id)

    async def fetch(sql, *a):
        return await db.fetch_cross(sql, *a)

    monkeypatch.setattr(rec, "TenantConn", db)
    monkeypatch.setattr(rec, "data_plane", gw)
    monkeypatch.setattr(svc, "data_plane", gw)  # purge_staging_quietly
    monkeypatch.setattr(rec, "_fetch", fetch)
    monkeypatch.setattr(rec.compute, "status", get_status)
    monkeypatch.setattr(rec.compute, "stop", stop_job)
    return SimpleNamespace(db=db, gw=gw, ray=ray)


def run(coro):
    return asyncio.run(coro)


def test_pending_job_stays_queued(env):
    row = env.db.seed(S.QUEUED)
    run(rec.reconcile_inflight_imports(T0))
    assert row["status"] == S.QUEUED


def test_running_job_marks_import_running(env):
    row = env.db.seed(S.QUEUED)
    env.ray.status = R.RUNNING
    run(rec.reconcile_inflight_imports(T0))
    assert row["status"] == S.RUNNING


def test_finished_job_moves_to_review_with_report_and_ttl(env):
    row = env.db.seed(S.QUEUED)  # a fast job is seen finished while still queued
    env.ray.status, env.gw.reports[str(row["id"])] = R.SUCCEEDED, OK
    run(rec.reconcile_inflight_imports(T0))
    assert row["status"] == S.AWAITING_REVIEW
    assert row["expires_at"] == T0 + IMPORT_REVIEW_TTL
    assert ImportReport.model_validate(row["report"]) == OK
    assert env.gw.purged_staging == []  # staging holds what review will read


def test_failed_report_fails_the_import_and_purges_staging(env):
    row = env.db.seed(S.RUNNING)
    env.ray.status, env.gw.reports[str(row["id"])] = R.SUCCEEDED, BAD
    run(rec.reconcile_inflight_imports(T0))
    assert row["status"] == S.FAILED and row["completed_at"] is not None
    assert row["report"]["issues"][0]["code"] == IssueCode.CORRUPT_IMAGE
    assert env.gw.purged_staging == [str(row["id"])]


def test_crashed_job_without_report_fails_with_internal_error(env):
    row = env.db.seed(S.RUNNING)
    env.ray.status = R.FAILED
    run(rec.reconcile_inflight_imports(T0))
    assert row["status"] == S.FAILED
    assert row["report"]["issues"][0]["code"] == IssueCode.INTERNAL_ERROR


TIMEOUT = timedelta(seconds=settings.dataset_limits.job_timeout_seconds)


def test_job_over_timeout_is_stopped_and_failed(env):
    row = env.db.seed(S.RUNNING, expires_at=T0 + TIMEOUT)
    run(rec.reconcile_inflight_imports(T0 + TIMEOUT - timedelta(seconds=1)))
    assert row["status"] == S.RUNNING
    run(rec.reconcile_inflight_imports(T0 + TIMEOUT))
    assert row["status"] == S.FAILED and env.ray.stopped == ["job"]
    assert row["report"]["issues"][0]["code"] == IssueCode.JOB_TIMEOUT


def test_timeout_runs_from_start_not_from_creation(env):
    now = T0 + timedelta(hours=2)  # created 2h ago, started just now
    row = env.db.seed(S.QUEUED, created_at=T0, expires_at=now + TIMEOUT)
    env.ray.status = R.RUNNING
    run(rec.reconcile_inflight_imports(now))
    assert row["status"] == S.RUNNING and row["expires_at"] == now + TIMEOUT and env.ray.stopped == []


def test_finished_job_polled_after_the_deadline_is_not_timed_out(env):
    row = env.db.seed(S.RUNNING, expires_at=T0)
    env.ray.status = R.SUCCEEDED
    env.gw.reports[str(row["id"])] = OK
    run(rec.reconcile_inflight_imports(T0 + TIMEOUT))
    assert row["status"] == S.AWAITING_REVIEW


def test_one_broken_import_does_not_block_the_rest(env, monkeypatch):
    bad, good = env.db.seed(S.RUNNING, ray_job_id="boom"), env.db.seed(S.RUNNING)
    env.ray.status = R.SUCCEEDED
    env.gw.reports[str(good["id"])] = OK

    async def get_status(job_id):
        if job_id == "boom":
            raise RuntimeError("ray down")
        return env.ray.status

    monkeypatch.setattr(rec.compute, "status", get_status)
    run(rec.reconcile_inflight_imports(T0))
    assert (bad["status"], good["status"]) == (S.RUNNING, S.AWAITING_REVIEW)


@pytest.mark.parametrize("answer", [RuntimeError("404 from the Ray dashboard"), "SOME_NEW_STATE"], ids=["error", "unknown_state"])
def test_the_deadline_still_ends_an_import_when_ray_gives_no_usable_status(env, monkeypatch, answer):
    row = env.db.seed(S.RUNNING, expires_at=T0 + TIMEOUT)

    async def get_status(job_id):
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(rec.compute, "status", get_status)
    run(rec.reconcile_inflight_imports(T0 + TIMEOUT - timedelta(seconds=1)))
    assert row["status"] == S.RUNNING  # not yet due: nothing is decided on a missing answer
    run(rec.reconcile_inflight_imports(T0 + TIMEOUT))
    assert row["status"] == S.FAILED and row["report"]["issues"][0]["code"] == IssueCode.JOB_TIMEOUT


def test_expiry_purges_staging_only_after_the_row_change_succeeded(env, monkeypatch):
    row = env.db.seed(S.AWAITING_REVIEW, expires_at=T0)

    async def broken_audit(*a, **kw):
        raise RuntimeError("audit failed")

    monkeypatch.setattr(rec.audit, "record", broken_audit)
    run(rec.expire_stale_imports(T0 + timedelta(hours=1)))
    assert env.gw.purged_staging == []  # the review data is still there for the retry on the next tick


def test_a_failing_staging_purge_does_not_undo_the_expiry(env):
    row = env.db.seed(S.AWAITING_REVIEW, expires_at=T0)

    async def boom(*a):
        raise RuntimeError("s3 down")

    env.gw.purge_staging = boom
    run(rec.expire_stale_imports(T0 + timedelta(hours=1)))
    assert row["status"] == S.EXPIRED


@pytest.mark.parametrize("status", [S.AWAITING_UPLOAD, S.AWAITING_REVIEW])
def test_stale_import_expires_with_purge_and_audit(env, status):
    row = env.db.seed(status, expires_at=T0 + timedelta(hours=1))
    fresh = env.db.seed(status, expires_at=T0 + timedelta(hours=3))
    run(rec.expire_stale_imports(T0 + timedelta(hours=2)))
    assert (row["status"], fresh["status"]) == (S.EXPIRED, status)
    assert env.gw.purged_staging == [str(row["id"])]
    assert env.db.audits == [(SYSTEM_ACTOR, AuditAction.IMPORT_EXPIRED, str(row["id"]))]
    run(rec.expire_stale_imports(T0 + timedelta(hours=2)))  # nothing left to do
    assert len(env.db.audits) == 1


def test_a_commit_whose_lease_ran_out_goes_back_to_review(env):
    """Its process died after the claim; without this the import could be neither committed, discarded nor expired."""
    dead = env.db.seed(S.COMMITTING, expires_at=T0)
    live = env.db.seed(S.COMMITTING, expires_at=T0 + timedelta(hours=3))
    now = T0 + timedelta(hours=2)
    run(rec.expire_stale_imports(now))
    assert (dead["status"], live["status"]) == (S.AWAITING_REVIEW, S.COMMITTING)
    assert dead["expires_at"] == now + IMPORT_REVIEW_TTL
    assert env.gw.purged_staging == [] and env.db.audits == []  # staging is what review (or a commit retry) reads


def test_running_imports_never_expire(env):
    row = env.db.seed(S.RUNNING, expires_at=T0)
    run(rec.expire_stale_imports(T0 + timedelta(days=30)))
    assert row["status"] == S.RUNNING


def test_purge_removes_only_datasets_past_retention(env):
    old = env.db.dataset(T0)
    recent = env.db.dataset(T0 + timedelta(days=2))
    restored = env.db.dataset(None)
    env.gw.repos |= {str(old), str(recent), str(restored)}
    run(rec.purge_deleted_datasets(T0 + timedelta(days=DATASET_RETENTION_DAYS, seconds=1)))
    assert env.db.purged_rows == [old] and set(env.db.datasets) == {recent, restored}
    assert env.gw.deleted_repos == [str(old)] and env.gw.repos == {str(recent), str(restored)}


def test_purge_cleans_staging_of_open_imports(env):
    ds = env.db.dataset(T0)
    open_import = env.db.seed(S.AWAITING_UPLOAD, dataset_id=ds)
    run(rec.purge_deleted_datasets(T0 + timedelta(days=DATASET_RETENTION_DAYS + 1)))
    assert env.gw.purged_staging == [str(open_import["id"])]


def test_purge_skips_a_dataset_restored_after_the_listing(env, monkeypatch):
    ds = env.db.dataset(T0)

    async def list_then_restore(sql, *a):
        rows = await env.db.fetch_cross(sql, *a)
        env.db.datasets[ds] = None  # restore lands between the listing and the lock
        return rows

    monkeypatch.setattr(rec, "_fetch", list_then_restore)
    run(rec.purge_deleted_datasets(T0 + timedelta(days=DATASET_RETENTION_DAYS + 1)))
    assert env.gw.deleted_repos == [] and ds in env.db.datasets


def test_purge_is_idempotent_when_the_repo_is_already_gone(env):
    ds = env.db.dataset(T0)  # no repo in the gateway: it was deleted by an earlier, interrupted sweep
    later = T0 + timedelta(days=DATASET_RETENTION_DAYS + 1)
    run(rec.purge_deleted_datasets(later))
    run(rec.purge_deleted_datasets(later))
    assert ds not in env.db.datasets and env.db.purged_rows == [ds]


def test_purge_retries_after_a_gateway_failure(env, monkeypatch):
    ds = env.db.dataset(T0)
    later = T0 + timedelta(days=DATASET_RETENTION_DAYS + 1)
    original = env.gw.delete_dataset_repo

    async def broken(*a):
        raise RuntimeError("lakeFS down")

    monkeypatch.setattr(env.gw, "delete_dataset_repo", broken)
    run(rec.purge_deleted_datasets(later))
    assert ds in env.db.datasets  # rows kept, so the repo is never orphaned
    monkeypatch.setattr(env.gw, "delete_dataset_repo", original)
    run(rec.purge_deleted_datasets(later))
    assert ds not in env.db.datasets


def test_migration_defines_every_function_the_sweeps_call():
    sql = next(Path(__file__).parents[4].glob("migrations/*_dataset_sweeps.sql")).read_text()
    for call in (rec._EXPIRED_TENANTS, rec._PURGEABLE, rec._PURGE_ROWS):
        name = re.search(r"(\w+)\(", call.split("FROM ")[-1] if "FROM" in call else call.removeprefix("SELECT ")).group(1)
        assert f"CREATE FUNCTION {name}(" in sql
    assert "inflight_import_tenant_ids" in rec._RUNNING_TENANTS


def test_sweeps_are_registered_as_pollers():
    registered = {fn for _, fn, _ in background.POLLERS}
    assert {rec.reconcile_inflight_imports, rec.expire_stale_imports, rec.purge_deleted_datasets} <= registered
