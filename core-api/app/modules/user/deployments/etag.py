"""Optimistic concurrency for a deployment (planning/05 Conventions): the ETag is a digest of the fields a stop, start,
redeploy or delete changes; `If-Match` on those calls refuses a caller whose copy is stale."""
import hashlib
from http import HTTPStatus

from app.core.problem import Problem
from app.modules.user.deployments.constants import PRECONDITION_FAILED_DETAIL, ProblemCode
from app.modules.user.deployments.schemas import DeploymentDetail

ETAG_DIGEST_CHARS = 16
ANY_ETAG = "*"


def etag_of(d: DeploymentDetail) -> str:
    raw = f"{d.id}|{d.status}|{d.training_run_id}|{d.rate_limit_per_minute}"
    return f'"{hashlib.sha256(raw.encode()).hexdigest()[:ETAG_DIGEST_CHARS]}"'


def check_if_match(if_match: str | None, current: DeploymentDetail) -> None:
    """No header: no precondition. Otherwise one of the listed tags (or `*`) must be the current one."""
    if if_match is None:
        return
    wanted = {t.strip().removeprefix("W/") for t in if_match.split(",")}
    if ANY_ETAG not in wanted and etag_of(current) not in wanted:
        raise Problem(HTTPStatus.PRECONDITION_FAILED, ProblemCode.PRECONDITION_FAILED, PRECONDITION_FAILED_DETAIL)
