"""Submitting the ingest job to Ray. The job is the `ray_jobs` package (top-level ray-jobs/), one entrypoint per job type.
It gets the `ingest` credential (staging bucket only) and the spec the datasets module built, nothing else: no
lakeFS credential (plan decision 4)."""
import shlex

from app.core import secret_files
from app.integrations import compute
from app.integrations.data_plane import access, job_package
from app.integrations.data_plane.job_catalog import ENTRYPOINTS, JobType
from app.integrations.job_spec import IngestSpec


# Fetches the package zip, makes it importable and runs the entrypoint as __main__ (argv: url, module, params json).
# ponytail: stands in for runtime_env working_dir, which ray_client.submit_raw_job does not take yet; drop it then,
# or once the package is baked into the Ray image (entrypoint becomes a plain `python -m`).
_BOOTSTRAP = (
    "import io, runpy, sys, tempfile, urllib.request, zipfile; d = tempfile.mkdtemp(); "
    "zipfile.ZipFile(io.BytesIO(urllib.request.urlopen(sys.argv[1], timeout=60).read())).extractall(d); "
    "sys.path.insert(0, d); module = sys.argv[2]; sys.argv = [module, *sys.argv[3:]]; "
    "runpy.run_module(module, run_name='__main__', alter_sys=True)"
)


def entrypoint(package_url: str, kind: JobType, spec_json: str) -> str:
    """Shell command Ray runs: the bootstrap, then the package url, the module and the job spec JSON as plain argv."""
    return shlex.join(["python", "-c", _BOOTSTRAP, package_url, f"{ENTRYPOINTS}.{kind.value}", spec_json])


async def submit_ingest(tenant_id: str, dataset_id: str, import_id: str, spec: IngestSpec, kind: JobType) -> str:
    """Submit the ingest job for an import under the spec's job_id (the caller's submission id, so it can stop the
    job even when the answer is lost); returns the Ray job id. The caller builds the spec from its own data."""
    await access.dataset_import(tenant_id, dataset_id, import_id)
    creds = secret_files.get(secret_files.INGEST)
    package_url = await job_package.presigned_url()
    return await compute.submit(compute.JobSpec(
        entrypoint=entrypoint(package_url, kind, spec.model_dump_json()),
        submission_id=spec.job_id,
        metadata={"tenant_id": tenant_id, "dataset_id": dataset_id, "import_id": import_id},
        num_cpus=spec.resources.cpu,
        memory=spec.resources.memory_bytes,
        resources={compute.CPU_POOL_RESOURCE: 1},  # ingest is CPU-only: keep it off the GPU worker
        timeout_seconds=spec.resources.max_runtime_s,
        env_vars={
            "STAGING_ENDPOINT": creds["endpoint"],
            "STAGING_ACCESS_KEY": creds["access_key"],
            "STAGING_SECRET_KEY": creds["secret_key"],
        },
    ))
