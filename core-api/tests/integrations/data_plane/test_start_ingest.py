"""start_ingest builds the job from Postgres and hands Ray the staging credential only."""
import asyncio
import base64
import json
import re
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

    async def submit(spec):
        sent.update(spec.model_dump())
        return "job-1"

    monkeypatch.setattr(ingest.compute, "submit", submit)
    assert asyncio.run(ingest.start_ingest(TENANT, DATASET, IMPORT, "ingest-1")) == "job-1"
    argv = base64.b64decode(re.search(r"b64decode\('([^']+)'\)\.decode", sent["entrypoint"]).group(1))
    job = json.loads(argv)
    assert job["prefix"] == layout.staging_prefix(TENANT, DATASET, IMPORT) and job["task"] == "tabular-regression"
    assert job["target"] == "y" and job["limits"]["upload_max_bytes"] == settings.dataset_limits.upload_max_bytes
    assert "base" not in job and set(sent["env_vars"]) == {"STAGING_ENDPOINT", "STAGING_ACCESS_KEY", "STAGING_SECRET_KEY"}
    assert asked == ["ingest"]  # never the staging secret, which also covers the lakeFS blockstore bucket
    assert sent["submission_id"] == "ingest-1"
    assert sent["num_cpus"] == settings.dataset_limits.ray_job_cpus
