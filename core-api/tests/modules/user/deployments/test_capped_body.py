"""routes.capped_body: the size cap holds when the length is not declared (chunked) or is declared wrong, and the body must be a JSON object."""
import asyncio

import pytest
from starlette.requests import Request

from app.core.problem import Problem
from app.core.settings import settings
from app.modules.user.deployments import routes
from app.modules.user.deployments.constants import ProblemCode


def body_of(data: bytes, content_length: str | None = None) -> Request:
    headers = [] if content_length is None else [(b"content-length", content_length.encode())]
    sent = iter([{"type": "http.request", "body": data, "more_body": False}])

    async def receive():
        return next(sent)

    return Request({"type": "http", "method": "POST", "headers": headers}, receive)


def capped(request):
    return asyncio.run(routes.capped_body(request))


def refused(request) -> Problem:
    with pytest.raises(Problem) as err:
        capped(request)
    return err.value


def test_a_body_without_a_declared_length_is_still_cut_at_the_cap():
    limit = settings.serving.max_body_bytes
    problem = refused(body_of(b'{"x":"' + b"a" * limit + b'"}'))
    assert (problem.status_code, problem.code) == (413, ProblemCode.BODY_TOO_LARGE) and str(limit) in problem.detail


def test_a_declared_length_under_the_cap_does_not_excuse_a_longer_body():
    limit = settings.serving.max_body_bytes
    assert refused(body_of(b'{"x":"' + b"a" * limit + b'"}', content_length="10")).status_code == 413


def test_the_cap_is_inclusive_and_a_body_at_it_is_parsed(monkeypatch):
    data = b'{"x":"' + b"a" * 20 + b'"}'
    monkeypatch.setattr(settings.serving, "max_body_bytes", len(data))
    assert capped(body_of(data, content_length=str(len(data)))) == {"x": "a" * 20}
    assert refused(body_of(data + b" ")).status_code == 413


@pytest.mark.parametrize("data,code", [(b"{", ProblemCode.BODY_NOT_JSON), (b"", ProblemCode.BODY_NOT_JSON), (b"[1]", ProblemCode.BODY_NOT_OBJECT), (b'"s"', ProblemCode.BODY_NOT_OBJECT)])
def test_a_body_that_is_not_a_json_object_is_422(data, code):
    problem = refused(body_of(data))
    assert (problem.status_code, problem.code) == (422, code)


def test_a_content_length_that_is_not_a_number_is_ignored_and_the_real_size_decides():
    assert capped(body_of(b"{}", content_length="abc")) == {}
