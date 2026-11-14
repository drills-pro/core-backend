"""serve_client: served_model, the poller body and an odd transport error."""
import asyncio

import httpx
import pytest

from app.core.enums import TaskType
from app.core.task_types import TaskInfo
from app.integrations import serve_client
from app.integrations.serve_client import ServeErrorCode
from tests.integrations.test_serve_shared import answer, call, puts, serve  # noqa: F401 - fixtures
from tests.support.task_types import task_row


def test_served_model_points_at_the_runs_artifacts_and_splits_providers_from_the_params(monkeypatch):
    async def run_model_url(tenant_id, project_id, run_id):
        assert (tenant_id, project_id, run_id) == ("t", "p", "r")
        return "s3://b/run/model.onnx"

    monkeypatch.setattr(serve_client.data_plane, "run_model_url", run_model_url)
    task = TaskInfo.model_validate(task_row(TaskType.IMAGE_CLASSIFICATION))
    model = asyncio.run(serve_client.served_model(task, "r", ["a"], "t", "p"))
    assert model.model_url == "s3://b/run/model.onnx"
    assert model.manifest_url == "s3://b/run/model.json" and model.tokenizer_url == "s3://b/run/tokenizer.json"
    assert model.class_names == ["a"] and "providers" not in model.params and model.providers


def test_the_serving_poller_ensures_the_apps(serve):  # noqa: F811
    asyncio.run(serve_client.ensure_serving())
    assert len(puts(serve)) == 1


def test_an_odd_transport_error_is_reported_as_unreachable_not_leaked(answer):  # noqa: F811
    answer(httpx.RemoteProtocolError("closed mid-response"))
    with pytest.raises(serve_client.ServeCallError) as err:
        call()
    assert err.value.code == ServeErrorCode.UNREACHABLE
