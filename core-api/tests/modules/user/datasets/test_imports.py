"""Import lifecycle: state machine, idempotent create, cap, stale base, commit under the dataset lock.

The database is an in-memory fake that answers the service's own SQL constants (so a query that changes
shape fails here), serialises FOR UPDATE like Postgres, and rolls a transaction back when it raises. Ray,
lakeFS and the staging store are fakes behind `import_service.data_plane`.
"""
import asyncio
import copy
import dataclasses
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.authz import Action, AuthzContext, TenantRole
from app.core.enums import TaskType
from app.core.problem import Problem
from app.core.settings import settings
from app.modules.user.datasets import import_service as svc
from app.modules.user.datasets import router as datasets
from app.modules.user.datasets.constants import (
    ACTIVE_STATUSES,
    TRANSITIONS,
    AuditAction,
    EntityType,
    ImportSource,
    ImportStatus,
    ProblemCode,
)
from app.integrations.data_plane.report import Stats
from app.modules.user.datasets.schemas import ImportCreate, ImportParams
from app.core.pagination import PageParams
from tests.support import PROJECT, TENANT, FakeConn, access, make_client, patch_tenant_conn

DS, KEY = uuid.uuid4(), "key-1"
STATS = {"file_count": 1, "total_bytes": 7, "row_count": 1}
REPORT = {"outcome": "ok", "stats": STATS}
CTX = AuthzContext(TENANT, "u1", TenantRole.OWNER, PROJECT, frozenset(Action))
S = ImportStatus
MIGRATION = next(Path(__file__).parents[4].glob("migrations/*_dataset_versions_imports.sql")).read_text()


class FakeDb:
    def __init__(self, task_type=TaskType.IMAGE_CLASSIFICATION):
        self.task_type, self.imports, self.versions, self.audits = task_type, {}, [], []
        self.lock, self.tenant_lock, self.fail_audit, self.fail_version = asyncio.Lock(), asyncio.Lock(), False, False
        self.fail_set_job = False
        self.access = access(roles=["editor"])

    def seed(self, status=S.AWAITING_REVIEW, **kw):
        row = {"id": uuid.uuid4(), "tenant_id": TENANT, "project_id": PROJECT, "dataset_id": DS, "source": ImportSource.UPLOAD,
               "status": status, "base_version_id": None, "params": {}, "report": REPORT,
               "staging_key": "p/", "ray_job_id": None, "expires_at": None, "idempotency_key": str(uuid.uuid4()),
               "created_at": datetime.now(UTC), "completed_at": None, **kw}
        self.imports[row["id"]] = row
        return row

    def seed_version(self):
        v = {"id": uuid.uuid4(), "dataset_id": DS, "number": len(self.versions) + 1, "import_id": uuid.uuid4(), "stats": {}, "created_at": datetime.now(UTC)}
        self.versions.append(v)
        return v

    def __call__(self, tenant_id):
        return _Tx(self)


class _Tx:
    def __init__(self, db):
        self.db, self.snapshot, self.locked, self.tenant_locked = db, None, False, False
        self.conn = _Conn(self)

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, *_):
        if exc_type and self.snapshot:
            self.db.imports, self.db.versions, self.db.audits = self.snapshot
        if self.locked:
            self.db.lock.release()
        if self.tenant_locked:
            self.db.tenant_lock.release()
        return False

    def mutating(self):
        self.snapshot = self.snapshot or copy.deepcopy((self.db.imports, self.db.versions, self.db.audits))


class _Conn:
    def __init__(self, tx):
        self.tx, self.db = tx, tx.db

    async def fetchrow(self, sql, *a):
        db = self.db
        if "project_access" in sql:
            return db.access
        if sql == svc._DATASET_TASK:
            return {"task_type": db.task_type}
        if sql == svc._IMPORT:
            row = db.imports.get(a[0])
            return row and dict(row)
        if sql == svc._BY_KEY:
            return next((dict(r) for r in db.imports.values() if r["idempotency_key"] == a[1]), None)
        if sql == svc._INSERT:
            self.tx.mutating()
            if any(r["idempotency_key"] == a[9] for r in db.imports.values()):
                return None
            keys = "id tenant_id project_id dataset_id source base_version_id params staging_key expires_at idempotency_key".split()
            row = db.seed(S.AWAITING_UPLOAD, report=None, **dict(zip(keys, a)))
            return dict(row)
        if sql == svc._TRANSITION:
            self.tx.mutating()
            row = db.imports[a[0]]
            if row["status"] != a[1]:
                return None
            row.update(status=a[2], expires_at=a[3], completed_at=a[4])
            return dict(row)
        if sql == svc._NEW_VERSION:
            if db.fail_version:
                raise RuntimeError("connection lost")
            self.tx.mutating()
            v = {"id": uuid.uuid4(), "dataset_id": a[1], "number": len(db.versions) + 1, "lakefs_commit": a[2], "import_id": a[3], "stats": a[4], "created_at": datetime.now(UTC)}
            db.versions.append(v)
            return dict(v)
        if sql == svc._VERSION_OF_IMPORT:
            return next(dict(v) for v in db.versions if v["import_id"] == a[0])
        if sql == svc._OTHER_COMMITTING:
            return next((dict(r) for r in db.imports.values()
                         if r["dataset_id"] == a[0] and r["id"] != a[1] and r["status"] == a[2]), None)
        if sql == svc._RESUME_COMMIT:  # NULL expires_at never compares as passed
            row = db.imports[a[0]]
            if row["status"] != a[2] or row["expires_at"] is None or row["expires_at"] > a[3]:
                return None
            self.tx.mutating()
            row["expires_at"] = a[1]
            return dict(row)
        raise AssertionError(f"unexpected fetchrow: {sql}")

    async def fetchval(self, sql, *a):
        db = self.db
        if sql == svc._SET_JOB:  # only while still queued: a cancel in between wins
            if db.fail_set_job:
                raise RuntimeError("connection lost")
            row = db.imports[a[0]]
            if row["status"] != a[2]:
                return None
            self.tx.mutating()
            row["ray_job_id"] = a[1]
            return a[0]
        if sql == svc._LOCK_DATASET:
            if not self.tx.locked:  # the route tests share one connection that is never exited
                await db.lock.acquire()
                self.tx.locked = True
            return DS
        if sql == svc._BASE_EXISTS:
            return 1 if any(v["id"] == a[0] for v in db.versions) else None
        if sql == svc._LATEST_VERSION:
            return db.versions[-1]["id"] if db.versions else None
        if sql == svc._ACTIVE_COUNT:
            count = sum(r["status"] in a[1] for r in db.imports.values())
            await asyncio.sleep(0)  # the answer takes time to arrive: without the tenant lock a concurrent create slips in here
            return count
        if sql == svc._DATASET_EXISTS:
            return 1
        if sql == svc._TENANT_LOCK:  # pg_advisory_xact_lock: held until the transaction ends
            await db.tenant_lock.acquire()
            self.tx.tenant_locked = True
            return None
        raise AssertionError(f"unexpected fetchval: {sql}")

    async def fetch(self, sql, *a):
        if sql == svc._RUNNING_OF_DATASET:
            return [dict(r) for r in self.db.imports.values() if r["dataset_id"] == a[0] and r["status"] in a[1]]
        return sorted(self.db.imports.values(), key=lambda r: r["created_at"], reverse=True)

    async def execute(self, sql, *a):
        self.tx.mutating()
        if sql == svc._RELEASE_COMMIT:
            if self.db.imports[a[0]]["status"] == a[2]:
                self.db.imports[a[0]]["expires_at"] = a[1]
        else:  # audit.record: tenant, project, actor, action, entity_type, entity_id
            if self.db.fail_audit:
                raise RuntimeError("audit failed")
            self.db.audits.append((a[3], a[4], a[5]))


class Gateway:
    """Stands in for app.integrations.data_plane; every call is recorded."""

    def __init__(self):
        self.calls, self.fail, self.stopped, self.n = [], None, [], 0
        self.args, self.hooks = {}, {}  # arguments of each call; callables run while the named call is in flight

    def __getattr__(self, name):
        async def op(tenant_id, dataset_id, *args):
            self.calls.append(name)
            self.args[name] = args
            if name in self.hooks:
                self.hooks[name]()
            await asyncio.sleep(0)  # yield, so two concurrent commits really interleave
            if name == self.fail:
                raise RuntimeError(f"{name} failed")
            if name == "commit_staged":
                self.n += 1
                return f"commit-{self.n}"
            return {"start_ingest": "ray-job", "presign_staging_put": "http://staging/put"}.get(name)

        return op


@pytest.fixture
def env(monkeypatch):
    db, gw, added = FakeDb(), Gateway(), []

    async def stop_job(job_id):
        gw.stopped.append(job_id)

    monkeypatch.setattr(svc, "TenantConn", db)
    monkeypatch.setattr(svc, "data_plane", gw)
    monkeypatch.setattr(svc.compute, "stop", stop_job)
    for counter in ("dataset_imports", "dataset_import_bytes"):
        monkeypatch.setattr(getattr(svc.usage, counter), "add", lambda n, attrs=None, c=counter: added.append((c, n)))
    db.usage = added
    return SimpleNamespace(db=db, gw=gw)


def run(coro):
    return asyncio.run(coro)


def create(body=None, key=KEY):
    return svc.create_import(CTX, PROJECT, DS, body or ImportCreate(source=ImportSource.UPLOAD), key)


def problem(coro, status, code):
    with pytest.raises(Problem) as err:
        run(coro)
    assert (err.value.status_code, err.value.code) == (status, code)


# --- state machine -------------------------------------------------------------------------------------

def test_every_status_has_transitions_and_terminal_ones_have_none():
    assert set(TRANSITIONS) == set(ImportStatus)
    assert all(not TRANSITIONS[s] for s in (S.SUCCEEDED, S.FAILED, S.CANCELLED, S.EXPIRED, S.DISCARDED))


def test_active_statuses_match_the_migration_index():
    active = re.search(r"dataset_imports_active_idx.*?status IN \(([^)]*)\)", MIGRATION, re.S).group(1)
    assert set(re.findall(r"'(\w+)'", active)) == {s.value for s in ACTIVE_STATUSES}


@pytest.mark.parametrize("frm", list(ImportStatus))
@pytest.mark.parametrize("to", list(ImportStatus))
def test_cas_moves_along_legal_edges_only(env, frm, to):
    row = env.db.seed(frm)
    conn = _Conn(_Tx(env.db))
    if to in TRANSITIONS[frm]:
        assert run(svc.cas(conn, row, to))["status"] == to
    else:
        problem(svc.cas(conn, row, to), 409, ProblemCode.INVALID_STATE)
        assert env.db.imports[row["id"]]["status"] == frm


def test_cas_loses_to_a_concurrent_move(env):
    row = env.db.seed(S.AWAITING_REVIEW)
    env.db.imports[row["id"]]["status"] = S.DISCARDED  # someone else got there between read and write
    problem(svc.cas(_Conn(_Tx(env.db)), row, S.COMMITTING), 409, ProblemCode.INVALID_STATE)


# --- create --------------------------------------------------------------------------------------------

def test_create_returns_an_upload_url_audits_and_counts(env):
    imp = run(create())
    assert (imp.status, imp.upload_url, imp.expires_at is not None) == (S.AWAITING_UPLOAD, "http://staging/put", True)
    assert env.db.audits == [(AuditAction.IMPORT_CREATED, EntityType.DATASET_IMPORT, str(imp.id))]
    assert env.db.usage == [("dataset_imports", 1)]


def test_only_a_writer_gets_the_upload_url_back_from_a_read(env):
    """A viewer could otherwise PUT its own bytes into the import an editor then starts."""
    imp = run(create())
    reader = dataclasses.replace(CTX, actions=frozenset({Action.DATASET_READ}))
    assert run(svc.get_import(CTX, PROJECT, DS, imp.id)).upload_url == "http://staging/put"
    assert run(svc.get_import(reader, PROJECT, DS, imp.id)).upload_url is None


def test_create_with_the_same_idempotency_key_returns_the_same_import(env):
    first, second = run(create()), run(create())
    assert first.id == second.id and len(env.db.imports) == 1 and len(env.db.audits) == 1 and len(env.db.usage) == 1


def test_create_is_capped_per_tenant_with_429(env):
    for _ in range(settings.dataset_limits.max_active_imports_per_tenant):
        env.db.seed(S.RUNNING)
    problem(create(), 429, ProblemCode.TOO_MANY_IMPORTS)
    env.db.imports.popitem()
    run(create())  # one slot free again


def test_finished_imports_do_not_count_against_the_cap(env):
    for _ in range(settings.dataset_limits.max_active_imports_per_tenant + 2):
        env.db.seed(S.SUCCEEDED)
    run(create())


def test_replaying_a_create_is_not_capped(env):
    first = run(create())
    for _ in range(settings.dataset_limits.max_active_imports_per_tenant):
        env.db.seed(S.RUNNING)
    assert run(create()).id == first.id


def test_wrong_task_type_for_the_project_is_422(env):
    problem(create(ImportCreate(source=ImportSource.UPLOAD, task_type=TaskType.OBJECT_DETECTION)), 422, ProblemCode.TASK_TYPE_MISMATCH)
    run(create(ImportCreate(source=ImportSource.UPLOAD, task_type=TaskType.IMAGE_CLASSIFICATION)))


def test_unsupported_task_type_is_422(env):
    env.db.task_type = TaskType.TEXT_CLASSIFICATION
    problem(create(), 422, "task_type_not_supported")


def test_a_project_without_a_task_type_cannot_import(env):
    env.db.task_type = None  # a project with no task type
    problem(create(), 422, ProblemCode.TASK_TYPE_MISSING)


def test_csv_needs_a_target_and_huggingface_needs_a_repo(env):
    env.db.task_type = TaskType.TABULAR_CLASSIFICATION
    problem(create(), 422, ProblemCode.INVALID_PARAMS)
    run(create(ImportCreate(source=ImportSource.UPLOAD, params=ImportParams(target="y"))))
    problem(create(ImportCreate(source=ImportSource.HUGGINGFACE), "k2"), 422, ProblemCode.INVALID_PARAMS)
    hf = run(create(ImportCreate(source=ImportSource.HUGGINGFACE, params=ImportParams(repo="a/b")), "k3"))
    assert hf.upload_url is None


def test_create_without_a_base_on_a_dataset_with_versions_is_422(env):
    env.db.seed_version()
    problem(create(), 422, ProblemCode.BASE_REQUIRED)
    assert not env.db.imports


def test_concurrent_creates_cannot_overshoot_the_cap(env):
    cap = settings.dataset_limits.max_active_imports_per_tenant
    for _ in range(cap - 1):
        env.db.seed(S.RUNNING)

    async def burst():
        return await asyncio.gather(*(create(key=f"k{i}") for i in range(3)), return_exceptions=True)

    results = run(burst())
    assert sum(isinstance(r, Problem) and r.status_code == 429 for r in results) == 2
    assert sum(r["status"] in ACTIVE_STATUSES for r in env.db.imports.values()) == cap


def test_base_version_must_belong_to_the_dataset(env):
    problem(create(ImportCreate(source=ImportSource.UPLOAD, base_version_id=uuid.uuid4())), 404, "not_found")
    v = env.db.seed_version()
    assert run(create(ImportCreate(source=ImportSource.UPLOAD, base_version_id=v["id"]))).base_version_id == v["id"]


# --- start ---------------------------------------------------------------------------------------------

def test_start_queues_the_import_and_submits_the_job(env):
    imp = run(create())
    started = run(svc.start_import(CTX, PROJECT, DS, imp.id))
    assert started.status == S.QUEUED
    assert env.db.imports[imp.id]["ray_job_id"] == "ray-job" and env.gw.calls[-2:] == ["create_dataset_repo", "start_ingest"]
    problem(svc.start_import(CTX, PROJECT, DS, imp.id), 409, ProblemCode.INVALID_STATE)


def test_start_refuses_a_stale_base(env):
    v1 = env.db.seed_version()
    imp = run(create(ImportCreate(source=ImportSource.UPLOAD, base_version_id=v1["id"])))
    env.db.seed_version()  # another import committed in between
    problem(svc.start_import(CTX, PROJECT, DS, imp.id), 409, ProblemCode.STALE_BASE)
    assert env.db.imports[imp.id]["status"] == S.AWAITING_UPLOAD and "start_ingest" not in env.gw.calls


def test_a_failed_job_submit_leaves_the_import_startable(env):
    imp = run(create())
    env.gw.fail = "start_ingest"
    with pytest.raises(RuntimeError):
        run(svc.start_import(CTX, PROJECT, DS, imp.id))
    assert env.db.imports[imp.id]["status"] == S.AWAITING_UPLOAD


def test_a_job_whose_id_cannot_be_saved_is_stopped_and_the_import_stays_startable(env):
    imp = run(create())
    env.db.fail_set_job = True
    with pytest.raises(RuntimeError):
        run(svc.start_import(CTX, PROJECT, DS, imp.id))
    assert env.gw.stopped == ["ray-job"] and env.db.imports[imp.id]["status"] == S.AWAITING_UPLOAD
    env.db.fail_set_job = False
    assert run(svc.start_import(CTX, PROJECT, DS, imp.id)).status == S.QUEUED


def test_a_job_that_may_have_been_submitted_is_stopped_before_the_retry(env):
    """Ray can accept the POST and the client still see an error: the retry must not leave two jobs on one prefix."""
    imp = run(create())
    env.gw.fail = "start_ingest"
    with pytest.raises(RuntimeError):
        run(svc.start_import(CTX, PROJECT, DS, imp.id))
    submission_id = env.gw.args["start_ingest"][1]
    assert str(imp.id) in submission_id and env.gw.stopped == [submission_id]
    env.gw.fail = None
    run(svc.start_import(CTX, PROJECT, DS, imp.id))
    assert env.gw.args["start_ingest"][1] != submission_id  # a fresh id, so Ray does not refuse it as a duplicate


def test_a_cancel_during_start_stops_the_job_instead_of_orphaning_it(env):
    imp = run(create())
    env.gw.hooks["start_ingest"] = lambda: env.db.imports[imp.id].update(status=S.CANCELLED)
    problem(svc.start_import(CTX, PROJECT, DS, imp.id), 409, ProblemCode.INVALID_STATE)
    assert env.gw.stopped == ["ray-job"]
    assert env.db.imports[imp.id]["status"] == S.CANCELLED and env.db.imports[imp.id]["ray_job_id"] is None


# --- cancel and discard --------------------------------------------------------------------------------

def test_cancel_stops_the_job_and_purges_staging(env):
    row = env.db.seed(S.RUNNING, ray_job_id="j1")
    assert run(svc.cancel_import(CTX, PROJECT, DS, row["id"])).status == S.CANCELLED
    assert env.gw.stopped == ["j1"] and env.gw.calls == ["purge_staging"]


@pytest.mark.parametrize("status", [S.AWAITING_REVIEW, S.COMMITTING, S.SUCCEEDED, S.FAILED, S.DISCARDED])
def test_cancel_is_only_for_work_in_progress(env, status):
    problem(svc.cancel_import(CTX, PROJECT, DS, env.db.seed(status)["id"]), 409, ProblemCode.INVALID_STATE)


def test_discard_purges_staging_and_audits(env):
    row = env.db.seed(S.AWAITING_REVIEW)
    assert run(svc.discard_import(CTX, PROJECT, DS, row["id"])).status == S.DISCARDED
    assert env.gw.calls == ["purge_staging"]
    assert env.db.audits == [(AuditAction.IMPORT_DISCARDED, EntityType.DATASET_IMPORT, str(row["id"]))]
    problem(svc.discard_import(CTX, PROJECT, DS, row["id"]), 409, ProblemCode.INVALID_STATE)


def test_cancelling_the_running_imports_of_a_deleted_dataset_stops_their_jobs(env):
    queued, running = env.db.seed(S.QUEUED, ray_job_id=None), env.db.seed(S.RUNNING, ray_job_id="j1")
    review, other_ds = env.db.seed(S.AWAITING_REVIEW), env.db.seed(S.RUNNING, dataset_id=uuid.uuid4())

    async def go():
        async with svc.TenantConn(TENANT) as conn:
            cancelled = await svc.cancel_running_imports(conn, DS)
        await svc.finish_cancelled(cancelled)

    run(go())
    assert [env.db.imports[r["id"]]["status"] for r in (queued, running, review, other_ds)] == [S.CANCELLED, S.CANCELLED, S.AWAITING_REVIEW, S.RUNNING]
    assert env.gw.stopped == ["j1"] and env.gw.calls == ["purge_staging", "purge_staging"]


def test_discard_keeps_staging_when_the_row_change_fails(env):
    row = env.db.seed(S.AWAITING_REVIEW)
    env.db.fail_audit = True
    with pytest.raises(RuntimeError):
        run(svc.discard_import(CTX, PROJECT, DS, row["id"]))
    assert env.db.imports[row["id"]]["status"] == S.AWAITING_REVIEW and "purge_staging" not in env.gw.calls


def test_discard_succeeds_even_when_the_purge_fails(env):
    row = env.db.seed(S.AWAITING_REVIEW)
    env.gw.fail = "purge_staging"
    assert run(svc.discard_import(CTX, PROJECT, DS, row["id"])).status == S.DISCARDED


def test_discard_only_after_review(env):
    problem(svc.discard_import(CTX, PROJECT, DS, env.db.seed(S.RUNNING)["id"]), 409, ProblemCode.INVALID_STATE)


# --- commit --------------------------------------------------------------------------------------------

def test_commit_creates_the_next_version_audits_and_counts_bytes(env):
    row = env.db.seed(S.AWAITING_REVIEW)
    version = run(svc.commit_import(CTX, PROJECT, DS, row["id"]))
    assert (version.number, version.import_id, version.stats) == (1, row["id"], Stats.model_validate(STATS).model_dump())
    assert env.db.imports[row["id"]]["status"] == S.SUCCEEDED and env.db.versions[0]["lakefs_commit"] == "commit-1"
    assert env.db.audits == [(AuditAction.IMPORT_COMMITTED, EntityType.DATASET_IMPORT, str(row["id"]))]
    assert env.db.usage == [("dataset_import_bytes", 7)]
    second = run(svc.commit_import(CTX, PROJECT, DS, env.db.seed(S.AWAITING_REVIEW, base_version_id=version.id)["id"]))
    assert second.number == 2


def test_double_commit_returns_the_same_version(env):
    row = env.db.seed(S.AWAITING_REVIEW)
    first, again = (run(svc.commit_import(CTX, PROJECT, DS, row["id"])) for _ in range(2))
    assert first.id == again.id and len(env.db.versions) == 1 and env.gw.calls.count("commit_staged") == 1
    assert len(env.db.audits) == 1


def test_concurrent_double_commit_commits_once(env):
    row = env.db.seed(S.AWAITING_REVIEW)

    async def both():
        return await asyncio.gather(*(svc.commit_import(CTX, PROJECT, DS, row["id"]) for _ in range(2)), return_exceptions=True)

    results = run(both())
    wins = [r for r in results if not isinstance(r, Exception)]
    losses = [r for r in results if isinstance(r, Problem)]
    assert len(wins) == 1 and len(losses) == 1 and (losses[0].status_code, losses[0].code) == (409, ProblemCode.INVALID_STATE)
    assert env.gw.calls.count("commit_staged") == 1 and len(env.db.versions) == 1
    assert run(svc.commit_import(CTX, PROJECT, DS, row["id"])).id == wins[0].id  # once done, the retry gets the version


def test_two_concurrent_commits_on_one_base_give_one_success_and_one_stale_base(env):
    v1 = env.db.seed_version()
    one, two = (env.db.seed(S.AWAITING_REVIEW, base_version_id=v1["id"]) for _ in range(2))

    async def both():
        return await asyncio.gather(*(svc.commit_import(CTX, PROJECT, DS, r["id"]) for r in (one, two)), return_exceptions=True)

    results = run(both())
    wins = [r for r in results if not isinstance(r, Exception)]
    losses = [r for r in results if isinstance(r, Problem)]
    assert len(wins) == 1 and len(losses) == 1 and (losses[0].status_code, losses[0].code) == (409, ProblemCode.STALE_BASE)
    assert [v["number"] for v in env.db.versions] == [1, 2]
    assert env.gw.calls.count("commit_staged") == 1
    loser = two if wins[0].import_id == one["id"] else one
    assert env.db.imports[loser["id"]]["status"] == S.AWAITING_REVIEW  # can still be discarded


def test_commit_without_a_base_on_a_dataset_with_versions_is_stale(env):
    """No base would rewrite index.parquet whole and drop the earlier rows; there is no overwrite mode."""
    env.db.seed_version()
    row = env.db.seed(S.AWAITING_REVIEW)
    problem(svc.commit_import(CTX, PROJECT, DS, row["id"]), 409, ProblemCode.STALE_BASE)
    assert "commit_staged" not in env.gw.calls and env.db.imports[row["id"]]["status"] == S.AWAITING_REVIEW


def test_two_base_less_imports_on_an_empty_dataset_cannot_both_commit(env):
    one, two = env.db.seed(S.AWAITING_REVIEW), env.db.seed(S.AWAITING_REVIEW)
    run(svc.commit_import(CTX, PROJECT, DS, one["id"]))
    problem(svc.commit_import(CTX, PROJECT, DS, two["id"]), 409, ProblemCode.STALE_BASE)


def test_a_failed_lakefs_commit_can_be_resumed_at_once(env):
    row = env.db.seed(S.AWAITING_REVIEW)
    env.gw.fail = "commit_staged"
    with pytest.raises(RuntimeError):
        run(svc.commit_import(CTX, PROJECT, DS, row["id"]))
    assert env.db.imports[row["id"]]["status"] == S.COMMITTING and not env.db.versions and not env.db.audits
    env.gw.fail = None
    assert run(svc.commit_import(CTX, PROJECT, DS, row["id"])).number == 1
    assert env.db.imports[row["id"]]["status"] == S.SUCCEEDED


def test_a_commit_that_failed_after_lakefs_resumes_instead_of_starting_over(env):
    """An orphan commit: lakeFS has the commit, Postgres has no version yet."""
    row = env.db.seed(S.AWAITING_REVIEW)
    env.db.fail_version = True
    with pytest.raises(RuntimeError, match="connection lost"):
        run(svc.commit_import(CTX, PROJECT, DS, row["id"]))
    assert env.db.imports[row["id"]]["status"] == S.COMMITTING and not env.db.versions
    env.db.fail_version = False
    assert run(svc.commit_import(CTX, PROJECT, DS, row["id"])).number == 1
    assert env.db.imports[row["id"]]["status"] == S.SUCCEEDED and len(env.db.versions) == 1
    # staged_commit is idempotent per import (tests/integrations/data_plane): both tries ask for the same import's commit
    assert env.gw.calls.count("commit_staged") == 2


def test_a_commit_in_progress_is_not_taken_over_until_its_lease_runs_out(env):
    soon = datetime.now(UTC) + timedelta(minutes=5)
    live = env.db.seed(S.COMMITTING, expires_at=soon)
    problem(svc.commit_import(CTX, PROJECT, DS, live["id"]), 409, ProblemCode.INVALID_STATE)
    assert "commit_staged" not in env.gw.calls
    env.db.imports[live["id"]]["expires_at"] = datetime.now(UTC) - timedelta(seconds=1)  # its process died
    assert run(svc.commit_import(CTX, PROJECT, DS, live["id"])).number == 1


def test_a_dead_commit_does_not_block_the_dataset_for_good(env):
    """Its process died after the claim: once the lease passed it goes back to review, and the next import commits."""
    dead = env.db.seed(S.COMMITTING, expires_at=datetime.now(UTC) - timedelta(seconds=1))
    other = env.db.seed(S.AWAITING_REVIEW)
    assert run(svc.commit_import(CTX, PROJECT, DS, other["id"])).number == 1
    assert env.db.imports[dead["id"]]["status"] == S.AWAITING_REVIEW
    problem(svc.commit_import(CTX, PROJECT, DS, dead["id"]), 409, ProblemCode.STALE_BASE)  # and it can still be discarded
    assert run(svc.discard_import(CTX, PROJECT, DS, dead["id"])).status == S.DISCARDED


def test_only_one_import_of_a_dataset_commits_at_a_time(env):
    env.db.seed(S.COMMITTING, expires_at=datetime.now(UTC) + timedelta(minutes=5))
    other = env.db.seed(S.AWAITING_REVIEW)
    problem(svc.commit_import(CTX, PROJECT, DS, other["id"]), 409, ProblemCode.INVALID_STATE)
    assert "commit_staged" not in env.gw.calls


def test_a_committed_import_has_its_staging_purged_and_a_purge_failure_is_tolerated(env):
    run(svc.commit_import(CTX, PROJECT, DS, env.db.seed(S.AWAITING_REVIEW)["id"]))
    assert env.gw.calls == ["commit_staged", "purge_staging"]
    env.gw.fail = "purge_staging"
    version = env.db.versions[-1]
    two = env.db.seed(S.AWAITING_REVIEW, base_version_id=version["id"])
    assert run(svc.commit_import(CTX, PROJECT, DS, two["id"])).number == 2


@pytest.mark.parametrize("status", [S.AWAITING_UPLOAD, S.QUEUED, S.RUNNING, S.FAILED, S.CANCELLED, S.DISCARDED, S.EXPIRED])
def test_commit_needs_a_reviewed_import(env, status):
    problem(svc.commit_import(CTX, PROJECT, DS, env.db.seed(status)["id"]), 409, ProblemCode.INVALID_STATE)
    assert "commit_staged" not in env.gw.calls


# --- routes --------------------------------------------------------------------------------------------

IMP = uuid.uuid4()
BASE = f"/v1/projects/{PROJECT}/datasets/{DS}/imports"
ROUTES = [  # (method, path, json body, needs write)
    ("POST", "", {"source": "upload"}, True),
    ("GET", "", None, False),
    ("GET", f"/{IMP}", None, False),
    ("POST", f"/{IMP}/start", None, True),
    ("POST", f"/{IMP}/cancel", None, True),
    ("POST", f"/{IMP}/commit", None, True),
    ("POST", f"/{IMP}/discard", None, True),
]
IDS = [f"{m} {p or '/'}".replace(str(IMP), "id") for m, p, _, _ in ROUTES]


@pytest.fixture
def call(monkeypatch, dev_headers, env):
    client = make_client(datasets)
    patch_tenant_conn(monkeypatch, env.db(None).conn, svc)  # deps' project_access and the service share the fake

    def send(acc, method, path, body=None, headers=None, qs=""):
        env.db.access = acc
        return client.request(method, f"{BASE}{path}{qs}", json=body, headers={"Idempotency-Key": "k", **(headers or {})})

    return send


@pytest.mark.parametrize("method,path,body,write", ROUTES, ids=IDS)
def test_viewer_cannot_write_and_a_dataset_editor_can_reach_the_service(call, env, method, path, body, write):
    if write:
        r = call(access(), method, path, body)
        assert (r.status_code, r.json()["action"]) == (403, "dataset.write")
    else:
        assert call(access(), method, path, body).status_code in (200, 404)  # allowed; 404 only because no such import is seeded
    r = call(access(visibility="private", roles=["dataset_editor"]), method, path, body)
    assert r.status_code not in (401, 403)


@pytest.mark.parametrize("row", [access(visibility="private"), access(visibility=None, default_access=None, roles=None)])
@pytest.mark.parametrize("method,path,body,write", ROUTES, ids=IDS)
def test_no_access_is_404(call, row, method, path, body, write):
    r = call(row, method, path, body)
    assert (r.status_code, r.json()["code"]) == (404, "not_found")


@pytest.mark.parametrize("method,path", [(m, p) for m, p, _, _ in ROUTES if p])
def test_foreign_tenant_import_id_is_404(call, method, path):
    r = call(access(roles=["editor"]), method, path)  # nothing seeded: RLS hides the other tenant's row
    assert (r.status_code, r.json()["code"]) == (404, "not_found")


def test_create_route_needs_the_idempotency_key_and_returns_201(call, env):
    editor = access(roles=["editor"])
    r = call(editor, "POST", "", {"source": "upload"})
    assert (r.status_code, r.json()["status"], r.json()["upload_url"]) == (201, "awaiting_upload", "http://staging/put")
    r = call(editor, "POST", "", {"source": "upload"}, headers={"Idempotency-Key": ""})
    assert r.status_code == 422


def test_full_lifecycle_through_the_routes(call, env):
    editor = access(roles=["editor"])
    imp = call(editor, "POST", "", {"source": "upload"}).json()
    assert call(editor, "POST", f"/{imp['id']}/start").status_code == 202
    row = env.db.imports[uuid.UUID(imp["id"])]
    row.update(status=S.AWAITING_REVIEW, report=REPORT)  # the reconciler's job (plan step 8)
    r = call(editor, "POST", f"/{imp['id']}/commit")
    assert (r.status_code, r.json()["number"]) == (200, 1) and "lakefs_commit" not in r.json()
    assert call(editor, "GET", f"/{imp['id']}").json()["status"] == "succeeded"
    assert len(call(editor, "GET", "").json()["items"]) == 1


def test_invalid_page_token_is_400(call):
    r = call(access(), "GET", "", qs="?page_token=garbage")
    assert (r.status_code, r.json()["code"]) == (400, "invalid_page_token")


def test_list_pages_with_a_cursor(env):
    for _ in range(3):
        env.db.seed(S.SUCCEEDED)
    page = run(svc.list_imports(CTX, PROJECT, DS, PageParams(page_size=2, page_token=None)))
    assert len(page.items) == 2 and page.next_page_token
