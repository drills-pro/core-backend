"""The gateway's contract: every operation takes a tenant plus ids, and a foreign tenant gets a 404 before
any backend (lakeFS, S3, MLflow) is touched."""
import asyncio
import inspect
import json
import uuid

import httpx
import pytest

from app.core.problem import Problem
from app.core.settings import settings
from app.integrations import data_plane
from app.integrations.data_plane import access, layout, lakefs, mlflow
from app.integrations.data_plane.mlflow import RunStatus
from tests.support import FakeConn, TenantCtx

TENANT, OTHER = str(uuid.uuid4()), str(uuid.uuid4())
DATASET, IMPORT, VERSION, PROJECT, RUN = (str(uuid.uuid4()) for _ in range(5))
COMMIT = "c0ffee"
ARGS = {
    "tenant_id": TENANT, "dataset_id": DATASET, "import_id": IMPORT, "dataset_version_id": VERSION,
    "version_id": VERSION, "project_id": PROJECT, "run_id": RUN, "path": "objects/a.jpg", "message": "m",
    "spec": None, "kind": None, "key": layout.INDEX_KEY, "limit": 5, "model_name": "clf", "version": "1", "stage": "Staging", "status": RunStatus.FINISHED,
}


def _kwargs(op, tenant_id):
    return {name: tenant_id if name == "tenant_id" else ARGS[name] for name in inspect.signature(op).parameters if name in ARGS}


@pytest.fixture
def owned(monkeypatch):
    """The tenant owns everything; only the version query returns a commit. Backends are real objects pointed at a stub."""
    conn = FakeConn(val=1, vals={"lakefs_commit": COMMIT})
    monkeypatch.setattr(access, "TenantConn", lambda tenant_id: TenantCtx(conn))
    monkeypatch.setattr(lakefs.secret_files, "get", lambda name: {"endpoint": "http://s3.test", "access_key": "k", "secret_key": "s"})
    return conn


@pytest.fixture
def backend_calls(monkeypatch):
    """Record every attempt to reach a backend."""
    calls = []

    def boom(*a, **kw):
        calls.append(a)
        raise AssertionError("backend reached")

    monkeypatch.setattr(lakefs, "_lakefs_api", boom)
    monkeypatch.setattr(lakefs, "_s3", boom)
    monkeypatch.setattr(mlflow.httpx, "AsyncClient", boom)
    return calls


def test_every_operation_is_covered_by_the_args_table():
    for op in data_plane.OPERATIONS:
        assert set(inspect.signature(op).parameters) - {"metrics"} <= set(ARGS), op.__name__
        assert "tenant_id" in inspect.signature(op).parameters


@pytest.mark.parametrize("op", data_plane.OPERATIONS, ids=lambda op: op.__name__)
def test_foreign_tenant_gets_404_and_no_backend_call(op, monkeypatch, backend_calls):
    monkeypatch.setattr(access, "TenantConn", lambda tenant_id: TenantCtx(FakeConn(val=None)))
    with pytest.raises(Problem) as err:
        asyncio.run(op(**_kwargs(op, OTHER)))
    assert err.value.status_code == 404
    assert not backend_calls


def test_malformed_ids_are_404(backend_calls):
    with pytest.raises(Problem) as err:
        asyncio.run(lakefs.create_dataset_repo(TENANT, "not-a-uuid"))
    assert err.value.status_code == 404 and not backend_calls


def test_presign_staging_put_targets_the_fixed_staging_key(owned):
    url = asyncio.run(lakefs.presign_staging_put(TENANT, DATASET, IMPORT))
    assert url.startswith(settings.public.staging_endpoint)
    assert f"{settings.data_plane.staging_bucket}/{layout.staging_prefix(TENANT, DATASET, IMPORT)}{layout.UPLOAD_KEY}" in url


def test_presign_get_reads_at_the_version_commit(owned):
    url = asyncio.run(lakefs.presign_get(TENANT, DATASET, VERSION, "objects/a.jpg"))
    assert url.startswith(settings.public.lakefs_endpoint)
    assert f"/{layout.repo_name(DATASET)}/{COMMIT}/objects/a.jpg" in url


@pytest.mark.parametrize("path", ["../x", "/abs", "a/../b", "a//b", "./a", ""])
def test_presign_get_rejects_bad_paths(owned, path):
    with pytest.raises(Problem) as err:
        asyncio.run(lakefs.presign_get(TENANT, DATASET, VERSION, path))
    assert err.value.status_code == 400


def _lakefs_stub(monkeypatch, handler):
    monkeypatch.setattr(
        lakefs, "_lakefs_api", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://lakefs.test/api/v1")
    )


def test_create_repo_is_idempotent_and_namespaced_by_tenant(owned, monkeypatch):
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(409)

    _lakefs_stub(monkeypatch, handler)
    asyncio.run(lakefs.create_dataset_repo(TENANT, DATASET))
    assert seen[0]["name"] == layout.repo_name(DATASET)
    assert seen[0]["storage_namespace"].endswith(f"/{TENANT}/{DATASET}/")


GOOD_OUT = [(layout.INDEX_KEY, 10), (f"{layout.OBJECTS_PREFIX}a/1.jpg", 5)]


def _staged(monkeypatch, entries):
    monkeypatch.setattr(lakefs, "_list_staged", lambda prefix: entries)


def _commit_handler(posted=None, history=(), links=None):
    """lakeFS stub: the commit history, a physical address per staged object, the link of that address, and the commit."""
    def handler(request):
        path = request.url.path
        if path.endswith("/staging/backing"):
            rel = request.url.params["path"]
            if request.method == "GET":
                return httpx.Response(200, json={"physical_address": f"s3://{BLOCKSTORE}/{TENANT}/{DATASET}/data/{rel},tok"})
            if links is not None:
                links.append((rel, json.loads(request.content)))
            return httpx.Response(200, json={})
        if path.endswith("/commits"):
            if request.method == "POST":
                if posted is not None:
                    posted.append(json.loads(request.content))
                return httpx.Response(201, json={"id": "abc"})
            return httpx.Response(200, json={"results": list(history)})
        return httpx.Response(404)
    return handler


class _CopyingS3:
    def __init__(self, copied):
        self.copied = copied

    def copy_object(self, **kw):
        self.copied.append(kw)
        return {"CopyObjectResult": {"ETag": '"etag"'}}


BLOCKSTORE = settings.data_plane.lakefs_blockstore_bucket


def test_commit_staged_copies_each_object_into_the_repo_and_commits_once(owned, monkeypatch):
    _staged(monkeypatch, GOOD_OUT)
    copied, links, posted = [], [], []
    monkeypatch.setattr(lakefs, "_s3", lambda secret, endpoint=None: _CopyingS3(copied))
    _lakefs_stub(monkeypatch, _commit_handler(posted, links=links))
    assert asyncio.run(lakefs.commit_staged(TENANT, DATASET, IMPORT, "import")) == "abc"
    staged = layout.staging_prefix(TENANT, DATASET, IMPORT) + layout.OUT_PREFIX
    assert sorted(c["CopySource"]["Key"] for c in copied) == sorted(staged + rel for rel, _ in GOOD_OUT)
    assert {c["CopySource"]["Bucket"] for c in copied} == {settings.data_plane.staging_bucket}
    assert {c["Bucket"] for c in copied} == {BLOCKSTORE}  # into the repo's storage, not left in staging
    assert all(c["Key"].endswith(",tok") for c in copied)  # lakeFS's key, token included
    assert sorted((rel, body["size_bytes"]) for rel, body in links) == sorted(GOOD_OUT)
    assert all(body["checksum"] == "etag" for _, body in links)
    assert posted == [{"message": "import", "metadata": {lakefs.IMPORT_ID_METADATA: IMPORT}}]


def test_a_retried_commit_returns_the_commit_an_earlier_try_made(owned, monkeypatch):
    posted = []
    history = [{"id": "other", "metadata": {}}, {"id": "orphan", "metadata": {lakefs.IMPORT_ID_METADATA: IMPORT}}]
    _lakefs_stub(monkeypatch, _commit_handler(posted, history))
    assert asyncio.run(lakefs.commit_staged(TENANT, DATASET, IMPORT, "import")) == "orphan"
    assert posted == []  # no second commit


def test_a_failed_link_makes_no_commit(owned, monkeypatch):
    _staged(monkeypatch, GOOD_OUT)
    posted = []
    monkeypatch.setattr(lakefs, "_s3", lambda secret, endpoint=None: _CopyingS3([]))
    handler = _commit_handler(posted)
    _lakefs_stub(monkeypatch, lambda request: httpx.Response(500) if request.method == "PUT" else handler(request))
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(lakefs.commit_staged(TENANT, DATASET, IMPORT, "import"))
    assert posted == []


def test_a_commit_that_outlives_the_timeout_fails(owned, monkeypatch):
    monkeypatch.setattr(settings.data_plane, "commit_timeout_seconds", 0.05)
    _staged(monkeypatch, GOOD_OUT)

    class Slow(_CopyingS3):
        def copy_object(self, **kw):
            import time  # noqa: PLC0415
            time.sleep(0.3)
            return super().copy_object(**kw)

    monkeypatch.setattr(lakefs, "_s3", lambda secret, endpoint=None: Slow([]))
    _lakefs_stub(monkeypatch, _commit_handler([]))
    with pytest.raises(TimeoutError):
        asyncio.run(lakefs.commit_staged(TENANT, DATASET, IMPORT, "import"))


LIMITS = settings.dataset_limits
HOSTILE_OUT = {
    "an extra file": [*GOOD_OUT, ("evil.sh", 1)],
    "no index": [(f"{layout.OBJECTS_PREFIX}a.jpg", 1)],
    "a parent path": [*GOOD_OUT, (f"{layout.OBJECTS_PREFIX}../index.parquet", 1)],
    "too many objects": [(layout.INDEX_KEY, 1), *[(f"{layout.OBJECTS_PREFIX}{i}.jpg", 1) for i in range(LIMITS.max_files + 1)]],
    "an oversized object": [(layout.INDEX_KEY, 1), (f"{layout.OBJECTS_PREFIX}a.jpg", LIMITS.image_max_bytes + 1)],
}


@pytest.mark.parametrize("entries", HOSTILE_OUT.values(), ids=HOSTILE_OUT.keys())
def test_commit_staged_refuses_output_outside_the_rules_before_touching_lakefs_data(owned, monkeypatch, entries):
    posted = []
    _staged(monkeypatch, entries)
    _lakefs_stub(monkeypatch, _commit_handler(posted))
    with pytest.raises(Problem) as err:
        asyncio.run(lakefs.commit_staged(TENANT, DATASET, IMPORT, "import"))
    assert (err.value.status_code, err.value.code) == (422, lakefs.INVALID_STAGED_OUTPUT) and posted == []


def test_tabular_output_is_allowed(owned, monkeypatch):
    assert lakefs._staged_violation([(layout.DATA_KEY, 3), (layout.SCHEMA_KEY, 1)]) is None


def test_delete_repo_tolerates_a_missing_repo_and_still_purges_the_namespace(owned, monkeypatch):
    purged, secrets = [], []
    _lakefs_stub(monkeypatch, lambda request: httpx.Response(404))
    monkeypatch.setattr(lakefs, "_delete_prefix", lambda bucket, prefix, client: purged.append((bucket, prefix, client)))
    monkeypatch.setattr(lakefs, "_s3", lambda secret, endpoint=None: secrets.append(secret) or secret)
    asyncio.run(lakefs.delete_dataset_repo(TENANT, DATASET))
    # The blockstore is purged with the `staging` credentials; docs/runtime-contract.md requires them to cover that bucket.
    assert purged == [(settings.data_plane.lakefs_blockstore_bucket, f"{TENANT}/{DATASET}/", "staging")]
    assert secrets == ["staging"]


def _mlflow_stub(monkeypatch, run_tags):
    real = httpx.AsyncClient

    def handler(request):
        if request.url.path.endswith("runs/get"):
            return httpx.Response(200, json={"run": {"data": {"tags": [{"key": k, "value": v} for k, v in run_tags.items()]}}})
        return httpx.Response(200, json={})

    monkeypatch.setattr(mlflow.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler)))


def test_finish_run_refuses_a_run_tagged_for_another_tenant(owned, monkeypatch):
    _mlflow_stub(monkeypatch, {mlflow.TAG_TENANT: OTHER, mlflow.TAG_PROJECT: PROJECT})
    with pytest.raises(Problem) as err:
        asyncio.run(mlflow.finish_run(TENANT, PROJECT, RUN, RunStatus.FINISHED))
    assert err.value.status_code == 404


def test_finish_run_accepts_its_own_run(owned, monkeypatch):
    _mlflow_stub(monkeypatch, {mlflow.TAG_TENANT: TENANT, mlflow.TAG_PROJECT: PROJECT})
    asyncio.run(mlflow.finish_run(TENANT, PROJECT, RUN, RunStatus.FINISHED))


def test_mlflow_names_are_namespaced_per_tenant():
    full = mlflow.registered_model_name(TENANT, PROJECT, "clf")
    assert full != mlflow.registered_model_name(OTHER, PROJECT, "clf")
    assert mlflow.display_name(full, TENANT, PROJECT) == "clf"
