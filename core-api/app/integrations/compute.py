"""The one door to compute (planning 07): submit a job, read its status and logs, stop it.

Two backends behind `settings.compute.backend`, chosen per call so a config flip needs no deploy of code:

  ray_dashboard  Ray's HTTP job API (`settings.ray.dashboard`, read per call), the shared cluster of today
  rayjob         a KubeRay `RayJob` custom resource created through the Kubernetes API; status is read from
                 the CR, so nothing polls a Ray head, and the control plane holds no credential to one

Callers pass a JobSpec and keep the returned job id (also the handle for `logs`); status values are Ray's job states either way.
"""
import json
import shlex
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

import httpx
from pydantic import BaseModel, Field

from app.core.settings import settings

KUBE_API = "https://kubernetes.default.svc"
KUBE_TOKEN = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
KUBE_CA = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
RAYJOB_PATH = "/apis/ray.io/v1/namespaces/{namespace}/rayjobs"
PODS_PATH = "/api/v1/namespaces/{namespace}/pods"
JOB_NAME_LABEL = "job-name"  # set by the Job controller on the submitter pod
CLUSTER_SELECTOR_LABEL = "ray.io/cluster"
KUBE_TIMEOUT_SECONDS = 10
DASHBOARD_TIMEOUT_SECONDS = 10
DASHBOARD_JOBS_PATH = "/api/jobs/"
DEFAULT_JOB_PREFIX = "job-"
CPU_POOL_RESOURCE = "cpu_pool"  # custom Ray resource only the CPU worker group advertises (k8s apps/ray raycluster.yaml)


class Backend(StrEnum):
    RAY_DASHBOARD = "ray_dashboard"
    RAYJOB = "rayjob"


class JobStatus(StrEnum):
    """Ray's job states, which RayJob's `status.jobStatus` reuses."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    STOPPED = "STOPPED"


class JobState(BaseModel):
    """Ray's answer for a job: its state and, when the backend knows it, when Ray started it (None = unknown).
    This is the trusted start time for billing; a job's own report is untrusted."""

    status: JobStatus
    started_at: datetime | None = None


class JobSpec(BaseModel):
    entrypoint: str
    metadata: dict[str, str] = Field(default_factory=dict)
    env_vars: dict[str, str] = Field(default_factory=dict)
    num_cpus: float = 0
    num_gpus: float = 0
    memory: int = 0  # bytes
    resources: dict[str, float] = Field(default_factory=dict)  # custom Ray resources, to pin a job to a worker group
    py_modules: list[str] = Field(default_factory=list)  # URIs of zips Ray downloads and puts on the job's import path
    requirements: list[str] = Field(default_factory=list)  # exact pins (app.core.requirements) Ray's uv runtime env installs; empty = the image's own packages
    submission_id: str | None = None  # the caller's id for the job, so it can stop it even if the answer is lost
    timeout_seconds: int = 0  # 0 = none; the RayJob backend enforces it as activeDeadlineSeconds


def requirements_runtime_env(requirements: list[str]) -> dict:
    """The runtime_env keys that make Ray install `requirements` with uv (planning/26); {} for none, so a package without
    requirements keeps the packages baked into the image. Shared by jobs here and Serve apps (serve_config)."""
    cfg = settings.compute.runtime_env
    if not requirements:
        return {}
    return {"uv": {"packages": requirements, "uv_pip_install_options": cfg.uv_options}, "config": {"setup_timeout_seconds": cfg.setup_timeout_seconds}}


def runtime_env(spec: JobSpec) -> dict:
    """The job's Ray runtime_env: what Ray sets up before the entrypoint runs."""
    env = {k: v for k, v in (("env_vars", spec.env_vars), ("py_modules", spec.py_modules)) if v}
    return {**env, **requirements_runtime_env(spec.requirements)}


def rayjob_manifest(spec: JobSpec, name: str) -> dict:
    cfg = settings.compute.rayjob
    body: dict = {
        "entrypoint": spec.entrypoint,
        "jobId": name,
        "submissionMode": "K8sJobMode",
        "shutdownAfterJobFinishes": cfg.shutdown_after_finish,
        "ttlSecondsAfterFinished": cfg.ttl_after_finished_seconds,
        "metadata": spec.metadata,
    }
    if cfg.cluster_name:  # a warm shared cluster; empty means one ephemeral cluster per job
        body["clusterSelector"] = {CLUSTER_SELECTOR_LABEL: cfg.cluster_name}
    else:
        body["rayClusterSpec"] = cfg.cluster_spec
    if env := runtime_env(spec):
        body["runtimeEnvYAML"] = json.dumps(env)  # JSON is YAML
    if spec.num_cpus:
        body["entrypointNumCpus"] = spec.num_cpus
    if spec.num_gpus:
        body["entrypointNumGpus"] = spec.num_gpus
    if spec.memory:
        body["entrypointMemory"] = spec.memory
    if spec.resources:
        body["entrypointResources"] = json.dumps(spec.resources)  # the CRD takes a JSON string
    if spec.timeout_seconds:
        body["activeDeadlineSeconds"] = spec.timeout_seconds
    return {
        "apiVersion": "ray.io/v1",
        "kind": "RayJob",
        "metadata": {"name": name, "labels": {k: v for k, v in spec.metadata.items() if v}},
        "spec": body,
    }


def rayjob_started_at(cr: dict) -> datetime | None:
    """`status.startTime` of a RayJob CR (RFC 3339), None until the job has started."""
    start = (cr.get("status") or {}).get("startTime")
    return datetime.fromisoformat(start) if start else None


def dashboard_started_at(info: dict) -> datetime | None:
    """`start_time` of a Ray job-API record: epoch milliseconds."""
    ms = info.get("start_time")
    return datetime.fromtimestamp(ms / 1000, UTC) if ms else None


def rayjob_status(cr: dict) -> JobStatus:
    """Ray's job state from a RayJob CR. `jobStatus` is empty until the submitter reports; a CR whose deployment
    failed (cluster never came up, deadline exceeded) never gets one, which is a failed job, not a pending one."""
    status = cr.get("status") or {}
    if status.get("jobStatus"):
        return JobStatus(status["jobStatus"])
    return JobStatus.FAILED if status.get("jobDeploymentStatus") == "Failed" else JobStatus.PENDING


def _kube() -> httpx.AsyncClient:
    """The Kubernetes API with this pod's ServiceAccount; tests replace it."""
    return httpx.AsyncClient(
        base_url=KUBE_API,
        headers={"Authorization": f"Bearer {KUBE_TOKEN.read_text().strip()}"},
        verify=str(KUBE_CA),
        timeout=KUBE_TIMEOUT_SECONDS,
    )


def _rayjobs() -> str:
    return RAYJOB_PATH.format(namespace=settings.compute.rayjob.namespace)


def _dashboard() -> httpx.AsyncClient:
    """Ray's HTTP job API at the configured dashboard (read per call, not frozen at import); tests replace it."""
    return httpx.AsyncClient(base_url=settings.ray.dashboard, timeout=DASHBOARD_TIMEOUT_SECONDS)


def _dashboard_payload(spec: JobSpec) -> dict:
    # Ray's job API has no deadline, so the entrypoint carries it (GNU timeout, present in the Ray images).
    entrypoint = f"timeout {spec.timeout_seconds} sh -c {shlex.quote(spec.entrypoint)}" if spec.timeout_seconds else spec.entrypoint
    payload: dict = {"entrypoint": entrypoint, "metadata": spec.metadata}
    if spec.num_gpus:
        payload["entrypoint_num_gpus"] = spec.num_gpus
    if spec.num_cpus:
        payload["entrypoint_num_cpus"] = spec.num_cpus
    if spec.memory:
        payload["entrypoint_memory"] = spec.memory
    if spec.resources:
        payload["entrypoint_resources"] = spec.resources
    if spec.submission_id:
        payload["submission_id"] = spec.submission_id
    if env := runtime_env(spec):
        payload["runtime_env"] = env
    return payload


class _DashboardBackend:
    async def submit(self, spec: JobSpec) -> str:
        async with _dashboard() as client:
            resp = await client.post(DASHBOARD_JOBS_PATH, json=_dashboard_payload(spec))
            resp.raise_for_status()
            return resp.json()["submission_id"]

    async def state(self, job_id: str) -> JobState:
        async with _dashboard() as client:
            resp = await client.get(f"{DASHBOARD_JOBS_PATH}{job_id}")
            resp.raise_for_status()
            info = resp.json()
            return JobState(status=JobStatus(info["status"]), started_at=dashboard_started_at(info))

    async def stop(self, job_id: str) -> None:
        async with _dashboard() as client:
            resp = await client.post(f"{DASHBOARD_JOBS_PATH}{job_id}/stop")
            resp.raise_for_status()

    async def logs(self, job_id: str) -> str:
        async with _dashboard() as client:
            resp = await client.get(f"{DASHBOARD_JOBS_PATH}{job_id}/logs")
            resp.raise_for_status()
            return resp.json()["logs"]


class _RayJobBackend:
    async def submit(self, spec: JobSpec) -> str:
        name = spec.submission_id or f"{DEFAULT_JOB_PREFIX}{uuid.uuid4().hex[:12]}"
        async with _kube() as client:
            resp = await client.post(_rayjobs(), json=rayjob_manifest(spec, name))
            resp.raise_for_status()
        return name

    async def state(self, job_id: str) -> JobState:
        async with _kube() as client:
            resp = await client.get(f"{_rayjobs()}/{job_id}")
            resp.raise_for_status()
            cr = resp.json()
            return JobState(status=rayjob_status(cr), started_at=rayjob_started_at(cr))

    async def stop(self, job_id: str) -> None:
        """Idempotent: a job that is already gone is stopped."""
        async with _kube() as client:  # deleting the CR stops the Ray job and tears down its cluster (planning 07)
            resp = await client.delete(f"{_rayjobs()}/{job_id}")
            if resp.status_code != httpx.codes.NOT_FOUND:
                resp.raise_for_status()

    async def logs(self, job_id: str) -> str:
        """The submitter pod's log (K8sJobMode runs `ray job submit` in a Job named like the RayJob, whose output
        is the entrypoint's). Empty once the Job is gone (ttl_after_finished_seconds) or before its pod runs."""
        pods = PODS_PATH.format(namespace=settings.compute.rayjob.namespace)
        async with _kube() as client:
            resp = await client.get(pods, params={"labelSelector": f"{JOB_NAME_LABEL}={job_id}"})
            resp.raise_for_status()
            items = resp.json().get("items", [])
            if not items:
                return ""
            pod = max(items, key=lambda p: p["metadata"]["creationTimestamp"])["metadata"]["name"]
            resp = await client.get(f"{pods}/{pod}/log")
            if resp.status_code == httpx.codes.BAD_REQUEST:  # container not started yet
                return ""
            resp.raise_for_status()
            return resp.text


class _Backend(Protocol):
    async def submit(self, spec: JobSpec) -> str: ...
    async def state(self, job_id: str) -> JobState: ...
    async def stop(self, job_id: str) -> None: ...
    async def logs(self, job_id: str) -> str: ...


def _backend() -> _Backend:
    """Chosen per call so a config flip needs no code change (and tests can flip it)."""
    return _RayJobBackend() if settings.compute.backend == Backend.RAYJOB else _DashboardBackend()


async def submit(spec: JobSpec) -> str:
    """Start the job and return its id (the submission id when the caller gave one)."""
    return await _backend().submit(spec)


async def job_state(job_id: str) -> JobState:
    return await _backend().state(job_id)


async def status(job_id: str) -> JobStatus:
    return (await job_state(job_id)).status


async def stop(job_id: str) -> None:
    await _backend().stop(job_id)


async def logs(job_id: str) -> str:
    """Whatever the job has printed so far (a running job's partial output too)."""
    return await _backend().logs(job_id)
