"""compute: GPU requests and the real Kubernetes client; job_lifecycle: fetch and cancelling a sweep."""
import asyncio
from pathlib import Path

import certifi
import httpx
import pytest

from app.core.settings import settings
from app.integrations import compute, job_lifecycle
from tests.integrations.test_job_lifecycle import JOB, S, T0, ray, table  # noqa: F401 - fixtures
from tests.support.fake_pool import RecordingConn, patch_pool

GPU_SPEC = compute.JobSpec(entrypoint="python train.py", num_gpus=1, submission_id="train-1")


def test_a_gpu_job_asks_ray_for_the_gpu_on_the_rayjob_backend(monkeypatch):
    monkeypatch.setattr(settings.compute, "backend", "rayjob")
    manifest = compute.rayjob_manifest(GPU_SPEC, "train-1")
    assert manifest["spec"]["entrypointNumGpus"] == 1 and "entrypointNumCpus" not in manifest["spec"]


def test_a_gpu_job_asks_ray_for_the_gpu_on_the_dashboard_backend():
    payload = compute._dashboard_payload(GPU_SPEC)
    assert payload["entrypoint_num_gpus"] == 1 and "entrypoint_num_cpus" not in payload


def test_the_kube_client_authenticates_with_the_service_account_token(tmp_path, monkeypatch):
    token = tmp_path / "token"
    token.write_text("abc123\n")
    monkeypatch.setattr(compute, "KUBE_TOKEN", token)
    monkeypatch.setattr(compute, "KUBE_CA", Path(certifi.where()))  # any readable CA bundle
    client = compute._kube()
    assert client.headers["Authorization"] == "Bearer abc123" and str(client.base_url).startswith(compute.KUBE_API)
    asyncio.run(client.aclose())


def test_rayjob_logs_are_empty_while_the_container_has_not_started(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pods"):
            return httpx.Response(200, json={"items": [{"metadata": {"name": "p", "creationTimestamp": "2026-01-01T00:00:00Z"}}]})
        return httpx.Response(400, json={"message": "ContainerCreating"})

    monkeypatch.setattr(settings.compute, "backend", "rayjob")
    monkeypatch.setattr(compute, "_kube", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://kube"))
    assert asyncio.run(compute.logs("j")) == ""


def test_fetch_runs_the_cross_tenant_listing_and_returns_its_rows(monkeypatch):
    class Conn(RecordingConn):
        async def fetch(self, sql, *args):
            self.calls.append(("fetch", sql, args))
            return [("t1",), ("t2",)]

    conn = Conn()
    pool = patch_pool(monkeypatch, job_lifecycle, conn)
    assert asyncio.run(job_lifecycle.fetch("SELECT x($1)", 5)) == [("t1",), ("t2",)]
    assert conn.calls == [("fetch", "SELECT x($1)", (5,))] and pool.released == 1


def test_a_cancelled_sweep_cancels_the_rows_it_was_still_following(table, ray):  # noqa: F811
    table.seed(S.RUNNING)
    started, cancelled = asyncio.Event(), []

    async def settle_forever(row, verdict):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(row["id"])
            raise

    async def go():
        sweep = asyncio.create_task(job_lifecycle.sweep(JOB, settle_forever, T0))
        await started.wait()
        sweep.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sweep
        await asyncio.sleep(0.01)

    asyncio.run(go())
    assert len(cancelled) == 1
