"""start_ingest builds the job from Postgres and hands Ray the staging credential only."""
import asyncio
import json
import shlex
import uuid

from app.core.settings import settings
from app.integrations.data_plane import access, ingest, layout
from tests.support import FakeConn, TenantCtx

TENANT, DATASET, IMPORT = (str(uuid.uuid4()) for _ in range(3))


def test_job_params_carry_prefix_limits_and_the_staging_credential_but_no_lakefs_one(monkeypatch):
    row = {"source": "upload", "params": {"target": "y"}, "base_version_id": None, "task_type": "tabular-regression"}
    conn = FakeConn(row=row, val=1)
    monkeypatch.setattr(access, "TenantConn", lambda t: TenantCtx(conn))
    monkeypatch.setattr(ingest, "TenantConn", lambda t: TenantCtx(conn))
    asked = []
    monkeypatch.setattr(ingest.secret_files, "get", lambda name: asked.append(name) or {"endpoint": "http://s3", "access_key": "k", "secret_key": "s"})
    sent = {}

    async def submit(**kw):
        sent.update(kw)
        return "job-1"

    async def package_url():
        return "http://ray-side/jobs/ray-jobs-x.zip"

    monkeypatch.setattr(ingest.job_package, "presigned_url", package_url)
    monkeypatch.setattr(ingest.ray_client, "submit_raw_job", submit)
    assert asyncio.run(ingest.start_ingest(TENANT, DATASET, IMPORT, "ingest-1")) == "job-1"
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
