"""service.predict (the predict-key route, now deprecated): the refusals before Serve is called, the legacy HTTP forward, the metadata echo,
and the two poller purges that log what they dropped."""
import asyncio
import uuid
from http import HTTPStatus
from types import SimpleNamespace

import httpx
import pytest

from app.core import plan_limits
from app.core.authz import TenantStatus
from app.core.problem import Problem
from app.modules.user.deployments import calls, service
from app.modules.user.deployments.constants import METADATA_FIELD, ProblemCode, Serving
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
    state = SimpleNamespace(lookup=dict(LOOKUP), allowed=True, usage=[], posted=[], reply=httpx.Response(200, json={"predictions": [1]}, request=httpx.Request("POST", "http://serve/p")), checks=[])
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

    async def post_to_serve(route_prefix, body):
        state.posted.append((route_prefix, dict(body)))
        return state.reply, 3.0

    async def predict_limits(conn, tenant_id):
        return plan_limits.Predict(max_body_bytes=1000, max_inflight_per_deployment=3)

    patch_counted_conn(monkeypatch, FakeConn(row={"serving": Serving.LEGACY, "training_run_id": RUN}), service)
    monkeypatch.setattr(service, "get_pool", get_pool)
    monkeypatch.setattr(service.rate_limit, "check", check)
    monkeypatch.setattr(service, "record_usage", record_usage)
    monkeypatch.setattr(service, "_post_to_serve", post_to_serve)
    monkeypatch.setattr(service.plan_limits, "predict_limits", predict_limits)
    return state


def predict(body=None):
    return asyncio.run(service.predict(DEPLOYMENT, "key", {"text": "x"} if body is None else body))


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


def test_a_legacy_deployment_gets_the_body_without_metadata_which_is_echoed_back_and_the_call_is_metered(env):
    result = predict({"text": "x", METADATA_FIELD: {"trace": 1}})
    assert result == {"predictions": [1], METADATA_FIELD: {"trace": 1}}
    assert env.posted == [("/p", {"text": "x"})] and len(env.usage) == 1


def test_no_metadata_in_means_none_out(env):
    assert METADATA_FIELD not in predict()


def test_a_legacy_5xx_raises_and_is_not_metered(env):
    env.reply = httpx.Response(500, request=httpx.Request("POST", "http://serve/p"))
    with pytest.raises(httpx.HTTPStatusError):  # ponytail: the deprecated route lets it surface; /v1/predict maps it to 502
        predict()
    assert not env.usage


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
