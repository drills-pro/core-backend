"""A Problem raised mid-start keeps its status: only an unexpected error becomes the 503."""
import uuid

import pytest
from fastapi import HTTPException

from app.core.problem import Problem, not_found
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
