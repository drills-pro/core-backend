"""Starting the ingest job on Ray. The job is the `ray_jobs` package (top-level ray-jobs/), one entrypoint per job type.
It gets the `ingest` credential (staging bucket only) and presigned reads of the base version
and nothing else: no lakeFS credential (plan decision 4). Its parameters are built here from Postgres, so the
caller passes ids only."""
import shlex
import uuid

from app.core import secret_files
from app.core.enums import TABULAR_TASKS, ImportSource, TaskType
from app.core.problem import not_found
from app.core.settings import settings
from app.core.tenant_conn import TenantConn
from app.integrations import compute
from app.integrations.data_plane import access, job_package, layout, lakefs
from app.integrations.data_plane.job_catalog import CATALOG, JobType, ingest_job_type
from app.integrations.job_spec import IngestBase, IngestHf, IngestPayload, IngestSpec, Resources


# Fetches the package zip, makes it importable and runs the entrypoint as __main__ (argv: url, module, params json).
# ponytail: stands in for runtime_env working_dir, which ray_client.submit_raw_job does not take yet; drop it then,
# or once the package is baked into the Ray image (entrypoint becomes a plain `python -m`).
_BOOTSTRAP = (
    "import io, runpy, sys, tempfile, urllib.request, zipfile; d = tempfile.mkdtemp(); "
    "zipfile.ZipFile(io.BytesIO(urllib.request.urlopen(sys.argv[1], timeout=60).read())).extractall(d); "
    "sys.path.insert(0, d); module = sys.argv[2]; sys.argv = [module, *sys.argv[3:]]; "
    "runpy.run_module(module, run_name='__main__', alter_sys=True)"
)
HF_DEFAULT_REVISION = "main"  # the job records the commit sha it resolved in the report
_IMPORT = (
    "SELECT i.source, i.params, i.base_version_id, p.task_type FROM dataset_imports i "
    "JOIN projects p ON p.id = i.project_id WHERE i.id = $1 AND i.dataset_id = $2 AND i.tenant_id = $3"
)


def entrypoint(package_url: str, kind: JobType, spec_json: str) -> str:
    """Shell command Ray runs: the bootstrap, then the package url, the module and the job spec JSON as plain argv."""
    return shlex.join(["python", "-c", _BOOTSTRAP, package_url, CATALOG[kind].entrypoint, spec_json])


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
    params, task_type, limits = row["params"], row["task_type"], settings.dataset_limits
    payload = IngestPayload(
        bucket=settings.data_plane.staging_bucket,
        prefix=layout.staging_prefix(tenant_id, dataset_id, import_id),
        source=row["source"],
        task=task_type,
        target=params.get("target"),
        split=params.get("split"),
        limits=limits,
    )
    if row["source"] == ImportSource.HUGGINGFACE:
        payload.hf = IngestHf(repo=params["repo"], revision=params.get("revision") or HF_DEFAULT_REVISION, column=params.get("column"))
    if row["base_version_id"]:
        payload.base = IngestBase(**await _base(tenant_id, dataset_id, str(row["base_version_id"]), task_type))
    spec = IngestSpec(
        job_id=submission_id, tenant_id=tenant_id, payload=payload,
        resources=Resources(cpu=limits.ray_job_cpus, memory_bytes=limits.ray_job_memory_bytes, max_runtime_s=limits.job_timeout_seconds),
    )
    creds = secret_files.get(secret_files.INGEST)
    package_url = await job_package.presigned_url()
    command = entrypoint(package_url, ingest_job_type(row["source"], task_type), spec.model_dump_json())
    return await compute.submit(compute.JobSpec(
        entrypoint=command,
        submission_id=submission_id,
        metadata={"tenant_id": tenant_id, "dataset_id": dataset_id, "import_id": import_id},
        num_cpus=spec.resources.cpu,
        memory=spec.resources.memory_bytes,
        timeout_seconds=spec.resources.max_runtime_s,
        env_vars={
            "STAGING_ENDPOINT": creds["endpoint"],
            "STAGING_ACCESS_KEY": creds["access_key"],
            "STAGING_SECRET_KEY": creds["secret_key"],
        },
    ))
