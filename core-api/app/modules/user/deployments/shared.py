"""Shared deployments: answer a request with the deployment's model on the predictor apps (serve_client.predict).

The model's URLs are looked up once per (deployment, run) and remembered, so a request costs one small query, not an MLflow
round trip. A `main` re-deploy changes the run, so the next request builds the new entry and the old one ages out.
"""
import time
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass

from fastapi import HTTPException

from app.core import task_types
from app.core.settings import settings
from app.core.tenant_conn import TenantConn
from app.integrations import serve_client
from app.modules.user.deployments.constants import (
    DEFAULT_COMPUTE_TARGET,
    SERVE_ERROR_STATUS,
    SETTING_COMPUTE_TARGET,
    TARGET_CACHE_SIZE,
    TOO_MANY_REQUESTS_DETAIL,
)
from app.modules.user.projects import get_settings, get_task_type
from app.modules.user.training import get_run_model

_NO_MODEL = "the deployment's run has no servable model"


@dataclass(frozen=True)
class Target:
    app: serve_client.ComputeTarget
    run_id: uuid.UUID
    model: serve_client.ServedModel


_targets: OrderedDict[tuple[uuid.UUID, uuid.UUID], Target] = OrderedDict()
_inflight: dict[uuid.UUID, int] = {}  # per process: the cap is per core-api replica, not global


async def _target(tenant_id: str, project_id: uuid.UUID, deployment_id: uuid.UUID, run_id: uuid.UUID) -> Target:
    if (found := _targets.get((deployment_id, run_id))) is not None:
        _targets.move_to_end((deployment_id, run_id))
        return found
    async with TenantConn(tenant_id) as conn:
        run = await get_run_model(conn, project_id, run_id)
        task = await get_task_type(conn, project_id)
        info = None if task is None else await task_types.get(conn, task)
        settings_ = await get_settings(conn, project_id)
    if run is None or not run.servable or info is None or not info.servable:
        raise HTTPException(status_code=409, detail=_NO_MODEL)
    model = await serve_client.served_model(info, run.mlflow_run_id, run.class_names, tenant_id, str(project_id))
    target = Target(serve_client.ComputeTarget(settings_.get(SETTING_COMPUTE_TARGET, DEFAULT_COMPUTE_TARGET)), run_id, model)
    _targets[(deployment_id, run_id)] = target
    while len(_targets) > TARGET_CACHE_SIZE:
        _targets.popitem(last=False)
    return target


@contextmanager
def _slot(deployment_id: uuid.UUID):
    if _inflight.get(deployment_id, 0) >= settings.serving.max_inflight_per_deployment:
        raise HTTPException(status_code=429, detail=TOO_MANY_REQUESTS_DETAIL)
    _inflight[deployment_id] = _inflight.get(deployment_id, 0) + 1
    try:
        yield
    finally:
        _inflight[deployment_id] -= 1
        if not _inflight[deployment_id]:
            del _inflight[deployment_id]


async def answer(
    tenant_id: str, project_id: uuid.UUID, deployment_id: uuid.UUID, run_id: uuid.UUID, body: dict
) -> tuple[serve_client.Prediction, float]:
    """(the prediction, milliseconds it took) for the run the deployment serves. HTTPException on a failed call, with the status SERVE_ERROR_STATUS gives its code."""
    target = await _target(tenant_id, project_id, deployment_id, run_id)
    with _slot(deployment_id):
        start = time.perf_counter()
        try:
            prediction = await serve_client.predict(target.app, str(deployment_id), str(target.run_id), target.model, body)
        except serve_client.ServeCallError as e:
            raise HTTPException(status_code=SERVE_ERROR_STATUS[e.code], detail=f"{e.code}: {e.message}") from e
    return prediction, (time.perf_counter() - start) * 1000
