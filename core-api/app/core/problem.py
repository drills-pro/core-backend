"""RFC 9457 problem+json for every HTTPException, plus the X-Request-Id that ties a response to logs.

Every error raised in app/ is a `Problem` with a stable machine-readable `code` from a `ProblemCode` enum (the
shared kernel's below, each context's in its constants.py); tests/architecture enforces both. An HTTPException
raised by FastAPI or Starlette itself still gets a code derived from its status. `detail` stays a string.
"""
import re
import uuid
from enum import StrEnum
from http import HTTPStatus

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

MEDIA_TYPE = "application/problem+json"
REQUEST_ID_HEADER = "X-Request-Id"
_SAFE_ID = re.compile(r"[\w.-]{1,128}")  # a client-sent id goes into logs and headers: no free text


class ProblemCode(StrEnum):
    """What the shared kernel raises."""

    NOT_FOUND = "not_found"
    FORBIDDEN = "forbidden"
    UNAUTHENTICATED = "unauthenticated"  # no credentials at all
    BEARER_REQUIRED = "bearer_required"
    INVALID_TOKEN = "invalid_token"
    INVALID_API_KEY = "invalid_api_key"
    PLATFORM_ADMIN_REQUIRED = "platform_admin_required"
    AUTH_NOT_CONFIGURED = "auth_not_configured"
    NO_TENANT = "no_tenant"
    TENANT_REQUIRED = "tenant_required"  # the caller belongs to several tenants and named none


class Problem(HTTPException):
    def __init__(self, status_code: int, code: str, detail: str, **extra):
        super().__init__(status_code, detail)
        self.code = code
        self.extra = extra


def not_found() -> Problem:
    """The one 404. Authz denials and missing resources both raise it, so they cannot be told apart."""
    return Problem(HTTPStatus.NOT_FOUND, ProblemCode.NOT_FOUND, "not found")


def _problem(request: Request, exc: StarletteHTTPException) -> JSONResponse:
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
    app.add_exception_handler(StarletteHTTPException, _problem)  # also the router's own 404/405

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        sent = request.headers.get(REQUEST_ID_HEADER, "")
        request.state.request_id = sent if _SAFE_ID.fullmatch(sent) else uuid.uuid4().hex
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request.state.request_id
        return response
