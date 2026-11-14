"""predict.predict (the predict-key route, now deprecated): the refusals before Serve is called, the shared call, the metadata echo,
and the two poller purges that log what they dropped."""
import asyncio
import uuid
from http import HTTPStatus
from types import SimpleNamespace

import pytest

from app.core import plan_limits
from app.core.authz import TenantStatus
from app.core.problem import Problem
from app.integrations import serve_client
from app.integrations.serve_client import ServeErrorCode
from app.modules.user.deployments import calls, predict as predict_path
from app.modules.user.deployments.constants import METADATA_FIELD, ProblemCode
from app.modules.user.deployments.status import DeploymentStatus
from tests.support import FakeConn
from tests.support.counted_conn import patch_counted_conn

TENANT, PROJECT, RUN, DEPLOYMENT = str(uuid.uuid4()), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
LOOKUP = {"route_prefix": "/p", "status": DeploymentStatus.ACTIVE, "rate_limit_per_minute": 7, "tenant_id": TENANT, "project_id": PROJECT, "tenant_status": TenantStatus.ACTIVE}


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return self

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def env(monkeypatch):
    state = SimpleNamespace(lookup=dict(LOOKUP), allowed=True, usage=[], posted=[], logged=[], fail=None, checks=[])
    lookup = FakeConn()

    async def fetchrow(sql, *args):
        return state.lookup

    lookup.fetchrow = fetchrow

    async def get_pool():
        return _Pool(lookup)

    async def check(key, limit):
        state.checks.append((key, limit))
        return state.allowed

    async def record_usage(conn, **kw):
        state.usage.append(kw)

    async def call(tenant_id, project_id, deployment_id, run_id, body, size, max_inflight):
        state.posted.append((run_id, dict(body)))
        if state.fail:
            raise predict_path.ServeFailed(state.fail, "down", 3.0, run_id, size)
        prediction = serve_client.Prediction(predictions=[1], cold_load=False)
        return predict_path.Answered(run_id=run_id, request_bytes=size, prediction=prediction, elapsed_ms=3.0)

    async def record(*args, **kw):
        state.logged.append(kw)

    async def predict_limits(conn, tenant_id):
        return plan_limits.Predict(max_body_bytes=1000, max_inflight_per_deployment=3)

    patch_counted_conn(monkeypatch, FakeConn(val=RUN), predict_path)
    monkeypatch.setattr(predict_path, "get_pool", get_pool)
    monkeypatch.setattr(predict_path.rate_limit, "check", check)
    monkeypatch.setattr(predict_path, "record_usage", record_usage)
    monkeypatch.setattr(predict_path, "call", call)
    monkeypatch.setattr(predict_path.calls, "record", record)
    monkeypatch.setattr(predict_path.plan_limits, "predict_limits", predict_limits)
    return state


def predict(body=None):
    return asyncio.run(predict_path.predict(DEPLOYMENT, "key", {"text": "x"} if body is None else body))


def refused(**kw):
    with pytest.raises(Problem) as err:
        predict(**kw)
    return err.value


def test_an_unknown_deployment_or_a_wrong_key_is_401_before_anything_else(env):
    env.lookup = None
    problem = refused()
    assert (problem.status_code, problem.code) == (HTTPStatus.UNAUTHORIZED, ProblemCode.INVALID_PREDICT_KEY) and not env.checks and not env.posted


def test_a_suspended_tenant_is_503_and_does_not_use_up_the_rate_limit(env):
    env.lookup["tenant_status"] = TenantStatus.SUSPENDED
    assert refused().status_code == HTTPStatus.SERVICE_UNAVAILABLE and not env.checks


@pytest.mark.parametrize("status,code", [(DeploymentStatus.STOPPED, ProblemCode.STOPPED), (DeploymentStatus.DEPLOYING, ProblemCode.NOT_ACTIVE), (DeploymentStatus.FAILED, ProblemCode.NOT_ACTIVE)])
def test_a_deployment_that_does_not_serve_is_409_and_is_not_counted_against_the_limit(env, status, code):
    env.lookup["status"] = status
    problem = refused()
    assert (problem.status_code, problem.code) == (HTTPStatus.CONFLICT, code) and not env.checks


def test_over_the_deployments_rate_limit_is_429_naming_the_limit_and_nothing_is_forwarded(env):
    env.allowed = False
    problem = refused()
    assert (problem.status_code, problem.code) == (HTTPStatus.TOO_MANY_REQUESTS, ProblemCode.RATE_LIMITED) and "7/min" in problem.detail
    assert env.checks == [(str(DEPLOYMENT), 7)] and not env.posted and not env.usage
    assert 1 <= int(problem.headers["Retry-After"]) <= 60  # seconds left in the fixed minute


@pytest.mark.parametrize("serving", ["shared", "legacy"])
def test_the_run_gets_the_body_without_metadata_which_is_echoed_back_and_the_call_is_logged_and_metered(env, serving):
    env.lookup["serving"] = serving  # a leftover legacy row is answered by the shared apps like any other
    result = predict({"text": "x", METADATA_FIELD: {"trace": 1}})
    assert result == {"predictions": [1], METADATA_FIELD: {"trace": 1}}
    assert env.posted == [(RUN, {"text": "x"})] and len(env.usage) == 1 and env.logged[0]["status"] == HTTPStatus.OK


def test_no_metadata_in_means_none_out(env):
    assert METADATA_FIELD not in predict()


def test_a_failed_serve_call_is_logged_with_its_code_and_not_metered(env):
    env.fail = ServeErrorCode.UNREACHABLE
    with pytest.raises(predict_path.ServeFailed):
        predict()
    assert not env.usage and env.logged[0]["error_code"] == ServeErrorCode.UNREACHABLE


def test_the_two_purges_log_only_when_something_was_dropped(monkeypatch, caplog):
    dropped = SimpleNamespace(n=3)
    conn = FakeConn()

    async def fetchval(sql, *args):
        return dropped.n

    conn.fetchval = fetchval

    async def get_pool():
        return _Pool(conn)

    monkeypatch.setattr(calls, "get_pool", get_pool)
    with caplog.at_level("INFO", logger=calls.logger.name):
        asyncio.run(calls.purge_test_calls())
        asyncio.run(calls.purge_predict_calls())
        assert [r.message for r in caplog.records] == ["purged 3 test calls", "purged 3 predict calls"]
        caplog.clear()
        dropped.n = 0
        asyncio.run(calls.purge_test_calls())
        asyncio.run(calls.purge_predict_calls())
    assert not caplog.records
