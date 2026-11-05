"""A Problem raised mid-start keeps its status: only an unexpected error becomes the 503."""
import uuid

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from app.core import problem
from app.core.problem import Problem, ProblemCode, not_found
from app.modules.user.datasets import import_service
from app.modules.user.training import create_service


def test_a_problem_is_a_fastapi_http_exception():
    assert isinstance(not_found(), HTTPException)


@pytest.mark.parametrize("start_failed", [create_service._start_failed, import_service._start_failed])
def test_start_failed_keeps_a_problem_and_wraps_anything_else(start_failed):
    problem = not_found()
    assert start_failed(problem, uuid.uuid4()) is problem
    wrapped = start_failed(RuntimeError("ray is down"), uuid.uuid4())
    assert isinstance(wrapped, Problem) and wrapped.status_code == 503


class Body(BaseModel):
    score: float = Field(gt=0, lt=1)
    tags: list[int] = []


def _app():
    app = FastAPI()
    problem.install(app)

    @app.post("/one")
    def one(body: Body):
        return body

    @app.post("/two")
    def two(body: Body):
        return body

    return app


@pytest.mark.parametrize("path", ["/one", "/two"])
@pytest.mark.parametrize("raw", ['{"score": NaN}', '{"score": Infinity, "tags": ["x"]}', '{"score": -Infinity}'])
def test_a_nan_or_infinity_in_a_body_is_a_422_problem_not_a_500(path, raw):
    r = TestClient(_app()).post(path, content=raw, headers={"Content-Type": "application/json"})
    body = r.json()
    assert r.status_code == 422 and r.headers["content-type"].startswith("application/problem+json")
    assert body["code"] == ProblemCode.VALIDATION_ERROR and body["request_id"] and body["detail"]
    assert all(set(e) == {"loc", "msg", "type"} for e in body["errors"])  # the offending input is not echoed
