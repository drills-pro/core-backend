"""The compute seam: the same JobSpec on both backends, RayJob against a fake Kubernetes API."""
import asyncio
import json

import httpx
import pytest

from app.core.settings import settings
from app.integrations import compute

SPEC = compute.JobSpec(
    entrypoint="python job.py", metadata={"tenant_id": "t1", "import_id": "i1"}, env_vars={"K": "v"},
    num_cpus=2, memory=1024, resources={"cpu_pool": 1}, submission_id="ingest-1", timeout_seconds=60, py_modules=["http://pkg/a.zip"],
)
RAYJOBS = "/apis/ray.io/v1/namespaces/ray/rayjobs"


class FakeKube:
    """Just enough of the Kubernetes API for RayJobs: one namespace, a dict of CRs."""

    def __init__(self):
        self.crs = {}
        self.seen = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append((request.method, request.url.path))
        name = request.url.path.rsplit("/", 1)[-1]
        if request.method == "POST":
            cr = json.loads(request.content)
            self.crs[cr["metadata"]["name"]] = cr
            return httpx.Response(201, json=cr)
        if name not in self.crs:
            return httpx.Response(404, json={})
        if request.method == "DELETE":
            del self.crs[name]
        return httpx.Response(200, json=self.crs.get(name, {}))


@pytest.fixture
def kube(monkeypatch):
    fake = FakeKube()
    monkeypatch.setattr(settings.compute, "backend", "rayjob")
    monkeypatch.setattr(compute, "_kube", lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(fake), base_url="http://kube"))
    return fake


def test_rayjob_submit_creates_a_cr_named_by_the_submission_id(kube):
    assert asyncio.run(compute.submit(SPEC)) == "ingest-1"
    cr = kube.crs["ingest-1"]
    assert cr["spec"]["entrypoint"] == "python job.py" and cr["spec"]["jobId"] == "ingest-1"
    assert json.loads(cr["spec"]["runtimeEnvYAML"]) == {"env_vars": {"K": "v"}, "py_modules": ["http://pkg/a.zip"]}
    assert cr["spec"]["entrypointNumCpus"] == 2 and cr["spec"]["entrypointMemory"] == 1024
    assert json.loads(cr["spec"]["entrypointResources"]) == {"cpu_pool": 1}
    assert cr["spec"]["activeDeadlineSeconds"] == 60 and "entrypointNumGpus" not in cr["spec"]
    assert cr["metadata"]["labels"] == SPEC.metadata
    assert "rayClusterSpec" in cr["spec"] and "clusterSelector" not in cr["spec"]


def test_rayjob_without_a_submission_id_gets_a_generated_name(kube):
    name = asyncio.run(compute.submit(SPEC.model_copy(update={"submission_id": None})))
    assert name.startswith(compute.DEFAULT_JOB_PREFIX) and name in kube.crs


def test_rayjob_on_a_warm_cluster_selects_it(kube, monkeypatch):
    monkeypatch.setattr(settings.compute.rayjob, "cluster_name", "warm")
    asyncio.run(compute.submit(SPEC))
    assert kube.crs["ingest-1"]["spec"]["clusterSelector"] == {compute.CLUSTER_SELECTOR_LABEL: "warm"}


@pytest.mark.parametrize("status, want", [
    ({}, compute.JobStatus.PENDING),
    ({"jobDeploymentStatus": "Initializing"}, compute.JobStatus.PENDING),
    ({"jobStatus": "RUNNING", "jobDeploymentStatus": "Running"}, compute.JobStatus.RUNNING),
    ({"jobStatus": "SUCCEEDED"}, compute.JobStatus.SUCCEEDED),
    ({"jobStatus": "STOPPED"}, compute.JobStatus.STOPPED),
    ({"jobDeploymentStatus": "Failed"}, compute.JobStatus.FAILED),  # never got a jobStatus: deadline or no cluster
])
def test_rayjob_status_is_read_from_the_cr(kube, status, want):
    asyncio.run(compute.submit(SPEC))
    kube.crs["ingest-1"]["status"] = status
    assert asyncio.run(compute.status("ingest-1")) == want


def test_rayjob_status_of_a_missing_cr_raises_so_the_reconciler_treats_it_as_unknown(kube):
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(compute.status("gone"))


def test_rayjob_stop_deletes_the_cr_and_is_idempotent(kube):
    asyncio.run(compute.submit(SPEC))
    asyncio.run(compute.stop("ingest-1"))
    asyncio.run(compute.stop("ingest-1"))
    assert "ingest-1" not in kube.crs and kube.seen.count(("DELETE", f"{RAYJOBS}/ingest-1")) == 2


def _dashboard(monkeypatch, handler):
    monkeypatch.setattr(settings.compute, "backend", "ray_dashboard")
    monkeypatch.setattr(compute, "_dashboard", lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ray"))


def test_dashboard_backend_maps_the_spec_onto_the_ray_job_api(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.content and json.loads(request.content)))
        if request.url.path.endswith("/stop"):
            return httpx.Response(200, json={"stopped": True})
        if request.method == "POST":
            return httpx.Response(200, json={"submission_id": "ray-1"})
        return httpx.Response(200, json={"status": "RUNNING"})

    _dashboard(monkeypatch, handler)
    assert asyncio.run(compute.submit(SPEC)) == "ray-1"
    assert seen[0] == ("POST", "/api/jobs/", {
        "entrypoint": "timeout 60 sh -c 'python job.py'", "metadata": SPEC.metadata, "entrypoint_num_cpus": 2,
        "entrypoint_memory": 1024, "entrypoint_resources": {"cpu_pool": 1}, "runtime_env": {"env_vars": {"K": "v"}, "py_modules": ["http://pkg/a.zip"]},
        "submission_id": "ingest-1"})
    assert asyncio.run(compute.status("ray-1")) == compute.JobStatus.RUNNING
    asyncio.run(compute.stop("ray-1"))
    assert seen[-1][:2] == ("POST", "/api/jobs/ray-1/stop")


def test_rayjob_logs_read_the_newest_submitter_pod(kube, monkeypatch):
    pods = "/api/v1/namespaces/ray/pods"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == pods:
            assert request.url.params["labelSelector"] == "job-name=ingest-1"
            return httpx.Response(200, json={"items": [
                {"metadata": {"name": "old", "creationTimestamp": "2026-01-01T00:00:00Z"}},
                {"metadata": {"name": "new", "creationTimestamp": "2026-01-02T00:00:00Z"}},
            ]})
        assert request.url.path == f"{pods}/new/log"
        return httpx.Response(200, text="metric:acc=0.9\n")

    monkeypatch.setattr(compute, "_kube", lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://kube"))
    assert asyncio.run(compute.logs("ingest-1")) == "metric:acc=0.9\n"


def test_rayjob_logs_are_empty_without_a_pod(kube, monkeypatch):
    monkeypatch.setattr(compute, "_kube", lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"items": []})), base_url="http://kube"))
    assert asyncio.run(compute.logs("ingest-1")) == ""


def test_dashboard_backend_logs_come_from_the_job_api(monkeypatch):
    _dashboard(monkeypatch, lambda r: httpx.Response(200, json={"logs": f"log of {r.url.path}"}))
    assert asyncio.run(compute.logs("ray-1")) == "log of /api/jobs/ray-1/logs"


def test_dashboard_url_is_read_per_call(monkeypatch):
    monkeypatch.setattr(settings.ray, "dashboard", "http://elsewhere:1")
    assert str(compute._dashboard().base_url) == "http://elsewhere:1"
