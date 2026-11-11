"""The fixture behind the POST /v1/predict tests: a real route and require_key, with the key table, the plan limits, the rate limit and
Serve faked and the predictions table in memory (tests/support/predictions.py)."""
import inspect
import uuid
from types import SimpleNamespace

from app.core import plan_limits
from app.core.enums import PlanTier
from app.core.authz import TenantStatus, generate_key, keys
from app.integrations import serve_client
from app.modules.user.deployments import calls, model_predict, prediction_service, prediction_store as store, prediction_worker, routes
from app.modules.user.deployments.constants import Serving
from app.modules.user.deployments.status import DeploymentStatus
from tests.support import make_client
from tests.support.counted_conn import patch_counted_conn
from tests.support.predictions import PredictionsConn

TENANT, PROJECT, MODEL, RUN, DEPLOYMENT = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
BODY = {"input": {"text": "hello", "n": 1.0}, "metadata": {"trace": "t1"}}
RESOLVED = {
    "id": DEPLOYMENT, "status": DeploymentStatus.ACTIVE, "serving": Serving.SHARED, "route_prefix": "/p", "training_run_id": RUN,
    "rate_limit_per_minute": 60, "mlflow_version": "3",
}


class Pool:
    def __init__(self):
        self.rows = {}

    def acquire(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def fetchrow(self, sql, key_hash):
        return self.rows.get(key_hash)

    async def execute(self, sql, key_id):
        return None  # api_key_touch


def build(monkeypatch):
    pool = Pool()

    async def get_pool():
        return pool

    monkeypatch.setattr(keys, "get_pool", get_pool)
    conn = PredictionsConn(row=RESOLVED)
    patch_counted_conn(monkeypatch, conn, model_predict, calls, prediction_worker, prediction_service)
    state = SimpleNamespace(conn=conn, pool=pool, served=[], usage=[], limited=False, limited_keys=set(), checks=[], plan_tier=PlanTier.FREE, answer=None, key=None)

    def add_key(models=(), tenant_status=TenantStatus.ACTIVE, project=PROJECT, rate_limit=None):
        key = generate_key()
        pool.rows[key.key_hash] = dict(
            key_id=uuid.uuid4(), tenant_id=TENANT, project_id=project, scopes=["predict"], model_ids=list(models), tenant_status=tenant_status,
            rate_limit_per_minute=rate_limit)
        return key.secret

    state.add_key = add_key
    state.key = add_key()

    async def predict_limits(conn, tenant_id):
        return plan_limits.Predict(max_body_bytes=200, max_inflight_per_deployment=3)

    async def check(key, limit):
        state.checks.append((key, limit))
        return not state.limited and key not in state.limited_keys

    async def plan_tier(conn, tenant_id):
        return state.plan_tier

    async def record_usage(conn, **kw):
        state.usage.append(kw)

    async def answer(*args):
        state.served.append(args)
        result = state.answer(*args)
        return await result if inspect.isawaitable(result) else result

    state.answer = lambda *a: (serve_client.Prediction(predictions=[{"label": "cat"}], cold_load=False), 12.5)
    monkeypatch.setattr(model_predict.plan_limits, "predict_limits", predict_limits)
    monkeypatch.setattr(model_predict.rate_limit, "check", check)
    monkeypatch.setattr(model_predict, "get_plan_tier", plan_tier)
    monkeypatch.setattr(model_predict, "record_usage", record_usage)
    monkeypatch.setattr(store, "record_usage", record_usage)
    monkeypatch.setattr(model_predict.shared, "answer", answer)

    async def tenants(sql):
        return conn.tenants_with_work()

    monkeypatch.setattr(prediction_worker.job_lifecycle, "fetch", tenants)
    client = make_client(routes.router, headers=None)

    def post(body=BODY, *, secret=None, model=MODEL, revision=None, project=PROJECT, key=None, extra=None, **kw):
        headers = {"Authorization": f"Bearer {secret or state.key}", "X-Project-Id": str(project), **(extra or {})}
        if key is not None:
            headers["Idempotency-Key"] = key
        if model is not None:
            headers["X-Model-Id"] = str(model)
        if revision is not None:
            headers["X-Model-Revision"] = revision
        return client.post("/v1/predict", json=body, headers=headers, **kw)

    def get(prediction_id, *, secret=None, project=PROJECT, path="", extra=None):
        headers = {"Authorization": f"Bearer {secret or state.key}", "X-Project-Id": str(project), **(extra or {})}
        return client.get(f"/v1/predict/{prediction_id}{path}", headers=headers)

    def cancel(prediction_id, *, secret=None, project=PROJECT):
        headers = {"Authorization": f"Bearer {secret or state.key}", "X-Project-Id": str(project)}
        return client.post(f"/v1/predict/{prediction_id}/cancel", headers=headers)

    state.post, state.get, state.cancel, state.client = post, get, cancel, client
    return state
