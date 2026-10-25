"""Ray Job Submission API client (req/07-compute-ray-k8s.md FR1).

Talks to Ray's HTTP job API directly rather than the Python SDK — core-api
doesn't need a Ray runtime, just to submit/poll/stop jobs on the cluster.
"""
import httpx

from app.core.settings import settings

RAY_DASHBOARD = settings.ray.dashboard


async def submit_raw_job(
    *, entrypoint: str, metadata: dict, num_gpus: float = 0, num_cpus: float = 0, memory: int = 0, env_vars: dict | None = None,
    submission_id: str | None = None,
) -> str:
    # FR2 (06-project-settings-deployment.md): compute_target maps to a Ray
    # resource request — the one place this touches the job payload shape,
    # not the template scripts.
    payload = {"entrypoint": entrypoint, "metadata": metadata}
    if num_gpus:
        payload["entrypoint_num_gpus"] = num_gpus
    if num_cpus:
        payload["entrypoint_num_cpus"] = num_cpus
    if memory:
        payload["entrypoint_memory"] = memory
    if submission_id:
        payload["submission_id"] = submission_id
    if env_vars:
        payload["runtime_env"] = {"env_vars": env_vars}
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{RAY_DASHBOARD}/api/jobs/",
            # FR1: tenant_id/project_id propagated to the job so they can
            # be pushed onto pod labels once this targets KubeRay (see
            # k8s/rayjob-example.yaml for the pod-label version).
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()["submission_id"]


async def get_status(job_id: str) -> str:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{RAY_DASHBOARD}/api/jobs/{job_id}")
        resp.raise_for_status()
        return resp.json()["status"]  # PENDING | RUNNING | SUCCEEDED | FAILED | STOPPED


async def get_logs(job_id: str) -> str:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{RAY_DASHBOARD}/api/jobs/{job_id}/logs")
        resp.raise_for_status()
        return resp.json()["logs"]


async def stop_job(job_id: str) -> None:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(f"{RAY_DASHBOARD}/api/jobs/{job_id}/stop")
        resp.raise_for_status()
