"""The ingest job's spec, built from the import row start_import already holds. The job gets presigned reads of the
base version and the staging prefix, nothing else (no lakeFS credential, plan decision 4)."""
from app.core.enums import ImportSource, TaskType
from app.core.settings import DatasetLimits, settings
from app.core.task_catalog import LABEL_COLUMN, DataKind
from app.integrations import data_plane
from app.integrations.data_plane import layout
from app.integrations.data_plane.data_kinds import LAYOUTS, ingest_job_type
from app.integrations.data_plane.job_catalog import JobType
from app.integrations.job_spec import IngestBase, IngestHf, IngestPayload, IngestSpec, Resources

HF_DEFAULT_REVISION = "main"  # the job records the commit sha it resolved in the report


async def _base(tenant_id: str, dataset_id: str, version_id: str, kind: DataKind) -> IngestBase:
    """Presigned reads of the base version's index (images) or data and schema (tables)."""
    key = LAYOUTS[kind].data_key
    if LAYOUTS[kind].has_objects:
        return IngestBase(index_url=await data_plane.presign_get(tenant_id, dataset_id, version_id, key))
    return IngestBase(
        data_url=await data_plane.presign_get(tenant_id, dataset_id, version_id, key),
        schema_url=await data_plane.presign_get(tenant_id, dataset_id, version_id, layout.SCHEMA_KEY),
    )


async def build(row: dict, task_type: TaskType, kind: DataKind, limits: DatasetLimits, submission_id: str) -> tuple[IngestSpec, JobType]:
    """The spec and job type for an import row (tenant_id, dataset_id, id, source, params, base_version_id)."""
    tenant_id, dataset_id, import_id = str(row["tenant_id"]), str(row["dataset_id"]), str(row["id"])
    params = row["params"]
    payload = IngestPayload(
        bucket=settings.data_plane.staging_bucket,
        prefix=layout.staging_prefix(tenant_id, dataset_id, import_id),
        source=row["source"],
        task=task_type,
        target=LABEL_COLUMN if task_type == TaskType.TEXT_CLASSIFICATION else params.get("target"),
        split=params.get("split"),
        limits=limits,
    )
    if row["source"] == ImportSource.HUGGINGFACE:
        payload.hf = IngestHf(repo=params["repo"], revision=params.get("revision") or HF_DEFAULT_REVISION, column=params.get("column"))
    if row["base_version_id"]:
        payload.base = await _base(tenant_id, dataset_id, str(row["base_version_id"]), kind)
    spec = IngestSpec(
        job_id=submission_id, tenant_id=tenant_id, payload=payload,
        resources=Resources(cpu=limits.ray_job_cpus, memory_bytes=limits.ray_job_memory_bytes, max_runtime_s=limits.job_timeout_seconds),
    )
    return spec, ingest_job_type(row["source"], kind)
