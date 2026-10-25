"""The one door to compute (planning 07): submit a job, read its status and logs, stop it.

Two backends behind `settings.compute.backend`, chosen per call so a config flip needs no deploy of code:

  ray_dashboard  Ray's HTTP job API (app/integrations/ray_client.py), the shared cluster of today
  rayjob         a KubeRay `RayJob` custom resource created through the Kubernetes API; status is read from
                 the CR, so nothing polls a Ray head, and the control plane holds no credential to one

Callers pass a JobSpec and keep the returned job id (also the handle for `logs`); status values are Ray's job states either way.
"""
import json
import uuid
from enum import StrEnum
from pathlib import Path
from typing import Protocol

import httpx
from pydantic import BaseModel, Field

from app.core.settings import settings
from app.integrations import ray_client

KUBE_API = "https://kubernetes.default.svc"
KUBE_TOKEN = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
KUBE_CA = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
RAYJOB_PATH = "/apis/ray.io/v1/namespaces/{namespace}/rayjobs"
PODS_PATH = "/api/v1/namespaces/{namespace}/pods"
JOB_NAME_LABEL = "job-name"  # set by the Job controller on the submitter pod
CLUSTER_SELECTOR_LABEL = "ray.io/cluster"
KUBE_TIMEOUT_SECONDS = 10
DEFAULT_JOB_PREFIX = "job-"


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


class JobSpec(BaseModel):
    entrypoint: str
    metadata: dict[str, str] = Field(default_factory=dict)
    env_vars: dict[str, str] = Field(default_factory=dict)
    num_cpus: float = 0
    num_gpus: float = 0
    memory: int = 0  # bytes
    submission_id: str | None = None  # the caller's id for the job, so it can stop it even if the answer is lost
    timeout_seconds: int = 0  # 0 = none; the RayJob backend enforces it as activeDeadlineSeconds


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
    if spec.env_vars:
        body["runtimeEnvYAML"] = json.dumps({"env_vars": spec.env_vars})  # JSON is YAML
    if spec.num_cpus:
        body["entrypointNumCpus"] = spec.num_cpus
    if spec.num_gpus:
        body["entrypointNumGpus"] = spec.num_gpus
    if spec.memory:
        body["entrypointMemory"] = spec.memory
    if spec.timeout_seconds:
        body["activeDeadlineSeconds"] = spec.timeout_seconds
    return {
        "apiVersion": "ray.io/v1",
        "kind": "RayJob",
        "metadata": {"name": name, "labels": {k: v for k, v in spec.metadata.items() if v}},
        "spec": body,
    }


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


class _DashboardBackend:
    async def submit(self, spec: JobSpec) -> str:
        # ponytail: Ray's job API has no deadline, so spec.timeout_seconds is not enforced here (the RayJob backend
        # does); a reconcile sweep that calls stop() on overdue jobs is the upgrade if the dashboard stays.
        return await ray_client.submit_raw_job(
            entrypoint=spec.entrypoint, metadata=spec.metadata, num_gpus=spec.num_gpus, num_cpus=spec.num_cpus,
            memory=spec.memory, env_vars=spec.env_vars or None, submission_id=spec.submission_id,
        )

    async def status(self, job_id: str) -> JobStatus:
        return JobStatus(await ray_client.get_status(job_id))

    async def stop(self, job_id: str) -> None:
        await ray_client.stop_job(job_id)

    async def logs(self, job_id: str) -> str:
        return await ray_client.get_logs(job_id)


class _RayJobBackend:
    async def submit(self, spec: JobSpec) -> str:
        name = spec.submission_id or f"{DEFAULT_JOB_PREFIX}{uuid.uuid4().hex[:12]}"
        async with _kube() as client:
            resp = await client.post(_rayjobs(), json=rayjob_manifest(spec, name))
            resp.raise_for_status()
        return name

    async def status(self, job_id: str) -> JobStatus:
        async with _kube() as client:
            resp = await client.get(f"{_rayjobs()}/{job_id}")
            resp.raise_for_status()
            return rayjob_status(resp.json())

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
    async def status(self, job_id: str) -> JobStatus: ...
    async def stop(self, job_id: str) -> None: ...
    async def logs(self, job_id: str) -> str: ...


def _backend() -> _Backend:
    """Chosen per call so a config flip needs no code change (and tests can flip it)."""
    return _RayJobBackend() if settings.compute.backend == Backend.RAYJOB else _DashboardBackend()


async def submit(spec: JobSpec) -> str:
    """Start the job and return its id (the submission id when the caller gave one)."""
    return await _backend().submit(spec)


async def status(job_id: str) -> JobStatus:
    return await _backend().status(job_id)


async def stop(job_id: str) -> None:
    await _backend().stop(job_id)


async def logs(job_id: str) -> str:
    """Whatever the job has printed so far (a running job's partial output too)."""
    return await _backend().logs(job_id)
