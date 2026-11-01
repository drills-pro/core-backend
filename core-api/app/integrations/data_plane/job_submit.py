"""What every Ray job core-api starts from a package has in common: the bootstrap command, the CPU-pool pin and the
`ingest` credential (staging bucket only) in the environment. Ingest and training differ only in package, module,
metadata and sizing."""
import shlex

from app.core import secret_files
from app.integrations import compute

# Fetches the package zip, makes it importable and runs the entrypoint as __main__ (argv: url, module, params json).
# ponytail: stands in for runtime_env working_dir, which compute.JobSpec does not take yet; drop it then,
# or once the package is baked into the Ray image (entrypoint becomes a plain `python -m`).
_BOOTSTRAP = (
    "import io, runpy, sys, tempfile, urllib.request, zipfile; d = tempfile.mkdtemp(); "
    "zipfile.ZipFile(io.BytesIO(urllib.request.urlopen(sys.argv[1], timeout=60).read())).extractall(d); "
    "sys.path.insert(0, d); module = sys.argv[2]; sys.argv = [module, *sys.argv[3:]]; "
    "runpy.run_module(module, run_name='__main__', alter_sys=True)"
)


def command(package_url: str, module: str, spec_json: str) -> str:
    """Shell command Ray runs: the bootstrap, then the package url, the module and the job spec JSON as plain argv."""
    return shlex.join(["python", "-c", _BOOTSTRAP, package_url, module, spec_json])


async def submit(
    *, package_url: str, module: str, spec_json: str, job_id: str, metadata: dict[str, str], num_cpus: float,
    timeout_seconds: int, memory: int = 0,
) -> str:
    """Submit the package's module under `job_id` (the caller's submission id, so it can stop the job even when the
    answer is lost); returns the Ray job id. CPU-only: pinned to the CPU worker group, never holds the GPU."""
    creds = secret_files.get(secret_files.INGEST)
    return await compute.submit(compute.JobSpec(
        entrypoint=command(package_url, module, spec_json),
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
        },
    ))
