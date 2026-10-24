"""Starting the ingest job on Ray. The job gets the `ingest` credential (staging bucket only) and presigned reads of the base version
and nothing else: no lakeFS credential (plan decision 4). Its parameters are built here from Postgres, so the
caller passes ids only."""
import base64
import json
import uuid
from pathlib import Path

from app.core import secret_files
from app.core.enums import TABULAR_TASKS, ImportSource, TaskType
from app.core.problem import not_found
from app.core.settings import settings
from app.core.tenant_conn import TenantConn
from app.integrations import compute
from app.integrations.data_plane import access, layout, lakefs

INGEST_SCRIPT = Path(__file__).parents[3] / "ingest_job" / "ingest.py"
HF_DEFAULT_REVISION = "main"  # the job records the commit sha it resolved in the report
_IMPORT = (
    "SELECT i.source, i.params, i.base_version_id, p.task_type FROM dataset_imports i "
    "JOIN projects p ON p.id = i.project_id WHERE i.id = $1 AND i.dataset_id = $2 AND i.tenant_id = $3"
)


async def _base(tenant_id: str, dataset_id: str, version_id: str, task_type: str) -> dict:
    """Presigned reads of the base version's index (images) or data and schema (tabular)."""
    if TaskType(task_type) in TABULAR_TASKS:
        return {
            "data_url": await lakefs.presign_get(tenant_id, dataset_id, version_id, layout.DATA_KEY),
            "schema_url": await lakefs.presign_get(tenant_id, dataset_id, version_id, layout.SCHEMA_KEY),
        }
    return {"index_url": await lakefs.presign_get(tenant_id, dataset_id, version_id, layout.INDEX_KEY)}


async def start_ingest(tenant_id: str, dataset_id: str, import_id: str, submission_id: str) -> str:
    """Submit the ingest job for an import under the caller's submission id (so it can stop the job even when the
    answer is lost); returns the Ray job id."""
    await access.dataset_import(tenant_id, dataset_id, import_id)
    async with TenantConn(tenant_id) as conn:
        row = await conn.fetchrow(_IMPORT, uuid.UUID(import_id), uuid.UUID(dataset_id), uuid.UUID(tenant_id))
    if row is None:
        raise not_found()
    params, task_type = row["params"], row["task_type"]
    job = {
        "bucket": settings.data_plane.staging_bucket,
        "prefix": layout.staging_prefix(tenant_id, dataset_id, import_id),
        "source": row["source"],
        "task": task_type,
        "target": params.get("target"),
        "split": params.get("split"),
        "limits": settings.dataset_limits.model_dump(),
    }
    if row["source"] == ImportSource.HUGGINGFACE:
        job["hf"] = {"repo": params["repo"], "revision": params.get("revision") or HF_DEFAULT_REVISION, "column": params.get("column")}
    if row["base_version_id"]:
        job["base"] = await _base(tenant_id, dataset_id, str(row["base_version_id"]), task_type)
    creds = secret_files.get(secret_files.INGEST)
    script = base64.b64encode(INGEST_SCRIPT.read_bytes()).decode()
    argv = base64.b64encode(json.dumps(job).encode()).decode()
    # Same shipping as every Ray script here: base64 sidesteps shell quoting. exec() under `python -c` runs as __main__.
    entrypoint = (
        f"python -c \"import base64, sys; sys.argv = ['ingest.py', base64.b64decode('{argv}').decode()]; "
        f"exec(base64.b64decode('{script}'))\""
    )
    return await compute.submit(compute.JobSpec(
        entrypoint=entrypoint,
        submission_id=submission_id,
        metadata={"tenant_id": tenant_id, "dataset_id": dataset_id, "import_id": import_id},
        num_cpus=settings.dataset_limits.ray_job_cpus,
        memory=settings.dataset_limits.ray_job_memory_bytes,
        timeout_seconds=settings.dataset_limits.job_timeout_seconds,
        env_vars={
            "STAGING_ENDPOINT": creds["endpoint"],
            "STAGING_ACCESS_KEY": creds["access_key"],
            "STAGING_SECRET_KEY": creds["secret_key"],
        },
    ))
