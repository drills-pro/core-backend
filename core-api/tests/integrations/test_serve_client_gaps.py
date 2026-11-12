"""serve_client: legacy remove, served_model, the read-only apps check, the poller body and an odd transport error."""
import asyncio

import httpx
import pytest

from app.core.enums import TaskType
from app.core.task_types import TaskInfo
from app.integrations import serve_client
from app.integrations.serve_client import ServeErrorCode
from app.integrations.serve_config import ComputeTarget, predictor_app
from tests.integrations.test_serve_shared import answer, call, puts, serve  # noqa: F401 - fixtures
from tests.support.fake_pool import RecordingConn, patch_pool
from tests.support.task_types import task_row


def test_remove_submits_a_ray_job_that_deletes_the_legacy_app_of_the_deployment(monkeypatch):
    submitted = []

    async def submit(spec):
        submitted.append(spec)
        return "job-1"

    monkeypatch.setattr(serve_client.compute, "submit", submit)
    kwargs = {"deployment_id": "d1", "project_id": "p1", "tenant_id": "t1"}
    assert asyncio.run(serve_client.remove(**kwargs, deploy_strategy="main")) == "job-1"
    assert asyncio.run(serve_client.remove(**kwargs, deploy_strategy="other")) == "job-1"
    main, per_deployment = submitted
    assert "serve.delete('deploy-project-p1-main')" in main.entrypoint
    assert "serve.delete('deploy-d1')" in per_deployment.entrypoint
    assert main.metadata == {"tenant_id": "t1", "project_id": "p1", "job_type": "serve-remove"}


def test_served_model_points_at_the_runs_artifacts_and_splits_providers_from_the_params(monkeypatch):
    async def run_model_url(tenant_id, project_id, run_id):
        assert (tenant_id, project_id, run_id) == ("t", "p", "r")
        return "s3://b/run/model.onnx"

    monkeypatch.setattr(serve_client.mlflow, "run_model_url", run_model_url)
    task = TaskInfo.model_validate(task_row(TaskType.IMAGE_CLASSIFICATION))
    model = asyncio.run(serve_client.served_model(task, "r", ["a"], "t", "p"))
    assert model.model_url == "s3://b/run/model.onnx"
    assert model.manifest_url == "s3://b/run/model.json" and model.tokenizer_url == "s3://b/run/tokenizer.json"
    assert model.class_names == ["a"] and "providers" not in model.params and model.providers


def test_apps_declared_is_true_only_when_every_predictor_app_is_live(serve):  # noqa: F811
    assert asyncio.run(serve_client.apps_declared()) is False
    serve.applications = {predictor_app(t): {} for t in ComputeTarget}
    assert asyncio.run(serve_client.apps_declared()) is True
    serve.applications = {predictor_app(ComputeTarget.GPU): {}}
    assert asyncio.run(serve_client.apps_declared()) is False


def test_apps_declared_is_false_not_an_error_when_ray_is_unreachable(serve, monkeypatch):  # noqa: F811
    def refuse(request):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(serve_client.httpx, "AsyncClient", lambda **kw: serve.real(transport=httpx.MockTransport(refuse), **kw))
    assert asyncio.run(serve_client.apps_declared()) is False


def test_the_serving_poller_ensures_the_apps(serve):  # noqa: F811
    asyncio.run(serve_client.ensure_serving())
    assert len(puts(serve)) == 1


def test_legacy_active_reads_the_database_function(monkeypatch):
    conn = RecordingConn(fetchval_result=True)
    patch_pool(monkeypatch, serve_client, conn)
    assert asyncio.run(serve_client._legacy_active()) is True
    assert conn.calls[0][1] == "SELECT legacy_deployments_active()"


def test_an_odd_transport_error_is_reported_as_unreachable_not_leaked(answer):  # noqa: F811
    answer(httpx.RemoteProtocolError("closed mid-response"))
    with pytest.raises(serve_client.ServeCallError) as err:
        call()
    assert err.value.code == ServeErrorCode.UNREACHABLE
