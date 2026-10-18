"""RFC 9457 problem+json for every HTTPException, plus the X-Request-Id that ties a response to logs.

Raise `Problem` to give an error a stable machine-readable `code`; a plain HTTPException gets one
derived from its status ("not_found", "unauthorized"). `detail` stays a string, as before.
"""
import re
import uuid
from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

MEDIA_TYPE = "application/problem+json"
REQUEST_ID_HEADER = "X-Request-Id"
_SAFE_ID = re.compile(r"[\w.-]{1,128}")  # a client-sent id goes into logs and headers: no free text


class Problem(HTTPException):
    def __init__(self, status_code: int, code: str, detail: str, **extra):
        super().__init__(status_code, detail)
        self.code = code
        self.extra = extra


def not_found() -> Problem:
    """The one 404. Authz denials and missing resources both raise it, so they cannot be told apart."""
    return Problem(404, "not_found", "not found")


def _problem(request: Request, exc: HTTPException) -> JSONResponse:
    status = HTTPStatus(exc.status_code)
    code = getattr(exc, "code", None) or status.phrase.lower().replace(" ", "_")
    # Whatever a handler wrote ("dataset not found in this project") would tell a caller more than
    # a missing resource should, so every 404 carries the same text.
    detail = "not found" if status is HTTPStatus.NOT_FOUND else str(exc.detail)
    body = {
        "type": f"urn:drills:problem:{code}",
        "title": status.phrase,
        "status": status.value,
        "detail": detail,
        "code": code,
        "request_id": request.state.request_id,
        **getattr(exc, "extra", {}),
    }
    return JSONResponse(body, status.value, headers=exc.headers, media_type=MEDIA_TYPE)


def install(app: FastAPI) -> None:
    app.add_exception_handler(HTTPException, _problem)

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        sent = request.headers.get(REQUEST_ID_HEADER, "")
        request.state.request_id = sent if _SAFE_ID.fullmatch(sent) else uuid.uuid4().hex
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request.state.request_id
        return response
