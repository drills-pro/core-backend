"""The old POST /v1/predict/{deployment_id} in shadow (planning/24, migration phase 1): Deprecation and Sunset on every answer, one counter
increment per call that got past the key check, and the body and status as before. service.predict is faked; its own tests are elsewhere."""
import uuid
from email.utils import parsedate_to_datetime
from http import HTTPStatus
from types import SimpleNamespace

import pytest

from app.core.problem import Problem
from app.core.settings import settings
from app.modules.user.deployments import routes, service
from app.modules.user.deployments.constants import ProblemCode
from tests.support import make_client

DEPLOYMENT = uuid.uuid4()
URL = f"/v1/predict/{DEPLOYMENT}"
KEY = {"X-Predict-Key": "k"}


@pytest.fixture
def legacy(monkeypatch):
    state = SimpleNamespace(authenticated=[], counted=[], outcome={"predictions": [1]})

    async def authenticate(deployment_id, key):
        state.authenticated.append(key)
        if isinstance(state.outcome, Problem) and state.outcome.code == ProblemCode.INVALID_PREDICT_KEY:
            raise state.outcome

    async def serve_authenticated(row, deployment_id, body):
        if isinstance(state.outcome, Exception):
            raise state.outcome
        return state.outcome

    class Counter:
        def add(self, value, labels):
            state.counted.append((value, labels))

    monkeypatch.setattr(service, "authenticate", authenticate)
    monkeypatch.setattr(service, "serve_authenticated", serve_authenticated)
    monkeypatch.setattr(routes.predict_metrics, "legacy_calls", Counter())
    state.post = lambda: make_client(routes.router).post(URL, json={}, headers=KEY)
    return state


def test_a_call_is_answered_as_before_marked_deprecated_and_counted_by_deployment(legacy):
    r = legacy.post()
    assert r.status_code == 200 and r.json() == {"predictions": [1]}
    assert r.headers["Deprecation"] == "true"
    assert parsedate_to_datetime(r.headers["Sunset"]) == settings.predict.legacy_sunset
    assert legacy.counted == [(1, {"deployment_id": str(DEPLOYMENT)})]


@pytest.mark.parametrize("status", [HTTPStatus.TOO_MANY_REQUESTS, HTTPStatus.BAD_GATEWAY])
def test_a_failed_call_carries_the_headers_keeps_its_own_and_is_counted(legacy, status):
    problem = Problem(status, ProblemCode.RATE_LIMITED, "slow down")
    problem.headers = {"Retry-After": "7"}
    legacy.outcome = problem
    r = legacy.post()
    assert r.status_code == status and r.headers["Retry-After"] == "7" and r.headers["Deprecation"] == "true" and "Sunset" in r.headers
    assert len(legacy.counted) == 1


def test_a_wrong_key_is_marked_but_not_counted_so_the_label_cannot_be_filled_by_strangers(legacy):
    legacy.outcome = Problem(HTTPStatus.UNAUTHORIZED, ProblemCode.INVALID_PREDICT_KEY, "invalid")
    r = legacy.post()
    assert r.status_code == 401 and r.headers["Deprecation"] == "true" and not legacy.counted


def test_a_wrong_key_is_refused_before_the_body_is_read(legacy):
    legacy.outcome = Problem(HTTPStatus.UNAUTHORIZED, ProblemCode.INVALID_PREDICT_KEY, "invalid")
    r = make_client(routes.router).post(URL, content=b"not json", headers={**KEY, "Content-Type": "application/json"})
    assert r.status_code == HTTPStatus.UNAUTHORIZED
