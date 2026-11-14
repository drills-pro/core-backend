"""What every Ray job core-api starts from a package has in common: the package as a runtime_env py_module, the CPU-pool pin and the
`ingest` credential (staging bucket only) in the environment. Ingest and training differ only in package, module,
metadata and sizing."""
import shlex

from app.core import secret_files
from app.integrations import compute

def command(module: str, spec_json: str) -> str:
    """Shell command Ray runs: the module as __main__, the job spec JSON as its only argument. The package is on the
    import path because the job's runtime_env lists its zip under py_modules."""
    return shlex.join(["python", "-m", module, spec_json])


async def submit(
    *, package_url: str, module: str, spec_json: str, job_id: str, metadata: dict[str, str], num_cpus: float,
    timeout_seconds: int, memory: int = 0, extra_env: dict[str, str] | None = None, bundle_urls: list[str] | None = None,
) -> str:
    """Submit the package's module under `job_id` (the caller's submission id, so it can stop the job even when the
    answer is lost); returns the Ray job id. CPU-only: pinned to the CPU worker group, never holds the GPU. Task bundles'
    zips (`bundle_urls`) go on the path next to the package, which is then the core that runs them."""
    creds = secret_files.get(secret_files.INGEST)
    return await compute.submit(compute.JobSpec(
        entrypoint=command(module, spec_json),
        py_modules=[package_url, *(bundle_urls or [])],
        submission_id=job_id,
        metadata=metadata,
        num_cpus=num_cpus,
        memory=memory,
        resources={compute.CPU_POOL_RESOURCE: 1},
        timeout_seconds=timeout_seconds,
        env_vars={
            "STAGING_ENDPOINT": creds["endpoint"],
            "STAGING_ACCESS_KEY": creds["access_key"],
            "STAGING_SECRET_KEY": creds["secret_key"],
            **(extra_env or {}),
        },
    ))
