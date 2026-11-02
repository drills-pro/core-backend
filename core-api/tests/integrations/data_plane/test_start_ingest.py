"""submit_ingest hands Ray the spec the datasets module built and the staging credential only."""
import asyncio
import json
import shlex
import uuid
from types import SimpleNamespace

from app.core.enums import TaskType
from app.core.settings import settings
from app.integrations import compute
from app.integrations.data_plane import access, ingest, job_submit, layout
from app.modules.user.datasets import ingest_spec
from tests.support import FakeConn, TenantCtx

TENANT, DATASET, IMPORT = (str(uuid.uuid4()) for _ in range(3))


def test_job_params_carry_prefix_limits_and_the_staging_credential_but_no_lakefs_one(monkeypatch):
    row = {"tenant_id": TENANT, "dataset_id": DATASET, "id": IMPORT, "source": "upload", "params": {"target": "y"}, "base_version_id": None}
    monkeypatch.setattr(access, "TenantConn", lambda t: TenantCtx(FakeConn(val=1)))
    asked = []
    monkeypatch.setattr(job_submit.secret_files, "get", lambda name: asked.append(name) or {"endpoint": "http://s3", "access_key": "k", "secret_key": "s"})
    sent = {}

    async def submit(spec):
        sent.update(spec.model_dump())
        return "job-1"

    async def resolve_stable(name):
        return SimpleNamespace(name=name, sha256="a" * 64)

    async def package_url(name, sha256):
        return "http://ray-side/jobs/ray-jobs-x.zip"

    monkeypatch.setattr(ingest.job_package, "resolve_stable", resolve_stable)
    monkeypatch.setattr(ingest.job_package, "presigned_url", package_url)
    monkeypatch.setattr(compute, "submit", submit)
    spec, kind = asyncio.run(ingest_spec.build(row, TaskType.TABULAR_REGRESSION, "ingest-1"))
    assert asyncio.run(ingest.submit_ingest(TENANT, DATASET, IMPORT, spec, kind)) == "job-1"
    argv = shlex.split(sent["entrypoint"])
    assert argv[:2] == ["python", "-c"] and argv[3:5] == ["http://ray-side/jobs/ray-jobs-x.zip", "ray_jobs.entrypoints.upload_csv"]
    spec = json.loads(argv[5])
    job = spec["payload"]
    assert spec["kind"] == "ingest" and spec["spec_version"] == 1 and spec["job_id"] == "ingest-1"
    assert job["prefix"] == layout.staging_prefix(TENANT, DATASET, IMPORT) and job["task"] == "tabular-regression"
    assert job["target"] == "y" and job["limits"]["upload_max_bytes"] == settings.dataset_limits.upload_max_bytes
    assert job["base"] is None and set(sent["env_vars"]) == {"STAGING_ENDPOINT", "STAGING_ACCESS_KEY", "STAGING_SECRET_KEY"}
    assert asked == ["ingest"]  # never the staging secret, which also covers the lakeFS blockstore bucket
    assert sent["submission_id"] == "ingest-1"
    assert sent["num_cpus"] == settings.dataset_limits.ray_job_cpus
    assert sent["resources"] == {compute.CPU_POOL_RESOURCE: 1}
