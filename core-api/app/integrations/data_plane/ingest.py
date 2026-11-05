"""Submitting the ingest job to Ray. The job is the `ray_jobs` package (top-level ray-jobs/), one entrypoint per job type.
It gets the `ingest` credential (staging bucket only) and the spec the datasets module built, nothing else: no
lakeFS credential (plan decision 4)."""
from app.core.enums import JobPackageName
from app.integrations.data_plane import access, job_package, job_submit
from app.integrations.data_plane.job_catalog import ENTRYPOINTS, JobType
from app.integrations.job_spec import IngestSpec


async def submit_ingest(tenant_id: str, dataset_id: str, import_id: str, spec: IngestSpec, kind: JobType) -> str:
    """Submit the ingest job for an import under the spec's job_id (the caller's submission id, so it can stop the
    job even when the answer is lost); returns the Ray job id. The caller builds the spec from its own data."""
    await access.dataset_import(tenant_id, dataset_id, import_id)
    package = await job_package.resolve_stable(JobPackageName.RAY_JOBS)
    package_url = await job_package.presigned_url(package.name, package.sha256)
    return await job_submit.submit(
        package_url=package_url, module=f"{ENTRYPOINTS}.{kind.value}", spec_json=spec.model_dump_json(), job_id=spec.job_id,
        metadata={"tenant_id": tenant_id, "dataset_id": dataset_id, "import_id": import_id},
        num_cpus=spec.resources.cpu, memory=spec.resources.memory_bytes, timeout_seconds=spec.resources.max_runtime_s,
    )
