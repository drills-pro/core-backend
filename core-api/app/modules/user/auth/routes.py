"""Who am I, and make my first tenant. Login itself is Zitadel's hosted OIDC flow: the SPA sends the
access token, core/auth.py verifies it, core/tenancy.py checks membership (ADR-021)."""
from http import HTTPStatus

from fastapi import APIRouter, Depends, Header, Request

from app.core import rate_limit
from app.core.auth import Claims, get_profile_claims
from app.core.authz import TenantRole, allowed_actions, public
from app.core.authz.policy import TENANT_ACTIONS
from app.core.idempotency import IDEMPOTENCY_HEADER
from app.core.problem import Problem
from app.modules.user.auth import service
from app.modules.user.auth.constants import (
    TENANT_KEY_MAX_LENGTH,
    TENANT_CREATE_IP_LIMIT,
    TENANT_CREATE_IP_RATE_KEY,
    TENANT_CREATE_USER_LIMIT,
    TENANT_CREATE_USER_RATE_KEY,
    ProblemCode,
)
from app.modules.user.auth.schemas import Me, TenantCreate, TenantCreated, TenantSummary

router = APIRouter(prefix="/v1", tags=["auth"])


def _client_ip(request: Request) -> str:
    # Traefik appends the peer it saw, so the last hop is the one the client cannot forge.
    # ponytail: assumes exactly one trusted proxy in front; revisit if a CDN is added.
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[-1].strip() or (request.client.host if request.client else "?")


def _capabilities(role: str) -> list[str]:
    return sorted(a.value for a in allowed_actions(TenantRole(role), None, None, ()) & TENANT_ACTIONS)


@router.get("/me", dependencies=[Depends(public)], response_model=Me)  # self-scoped: any valid token, no tenant yet
async def me(claims: Claims = Depends(get_profile_claims)):
    tenants = await service.sign_in(claims)
    # A user may belong to many tenants; capabilities are the tenant-level actions of the role held in each.
    return Me(
        sub=claims.subject, email=claims.email, email_verified=claims.email_verified, name=claims.display_name,
        tenants=[TenantSummary(**dict(t), capabilities=_capabilities(t["role"])) for t in tenants],
    )


@router.post("/tenants", status_code=201, dependencies=[Depends(public)], response_model=TenantCreated)  # creates the caller's own tenant
async def create_tenant(
    body: TenantCreate,
    request: Request,
    claims: Claims = Depends(get_profile_claims),
    idempotency_key: str = Header(..., alias=IDEMPOTENCY_HEADER, min_length=1, max_length=TENANT_KEY_MAX_LENGTH),
):
    """Tenant, owner membership and Default team in one transaction: nothing outside Postgres to undo."""
    if not claims.email_verified:
        raise Problem(HTTPStatus.FORBIDDEN, ProblemCode.EMAIL_NOT_VERIFIED, "verify your email before creating a tenant")
    replay = await service.find_created(claims.subject, idempotency_key)  # costs no rate-limit budget
    if replay is not None:
        return replay
    if not (
        await rate_limit.check(TENANT_CREATE_USER_RATE_KEY.format(claims.subject), TENANT_CREATE_USER_LIMIT)
        and await rate_limit.check(TENANT_CREATE_IP_RATE_KEY.format(_client_ip(request)), TENANT_CREATE_IP_LIMIT)
    ):
        raise Problem(HTTPStatus.TOO_MANY_REQUESTS, ProblemCode.TOO_MANY_ATTEMPTS, "too many attempts")

    return await service.create_tenant(claims, body, idempotency_key)
