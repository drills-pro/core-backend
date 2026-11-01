"""What other contexts may ask of tenant membership and of the people behind it. This context owns `principals`
(created with `tenant_members` in the shared-identity-pool migration) as well as `tenant_members`: a principal is
only ever read as "the person behind a membership" and written at sign-in on the way to one. Each function takes
the caller's connection, so it is one more query in the caller's transaction, under the same RLS."""
import uuid

from app.core import auth_log
from app.core.auth import ZITADEL_ISSUER, Claims
from app.core.authz import TenantRole
from app.core.enums import AuthAttribute, AuthEvent, AuthSpan
from app.core.metrics import auth as auth_metrics
from app.core.tracing import tracer

_IS_MEMBER = "SELECT 1 FROM tenant_members WHERE user_sub = $1"
_ADD_MEMBER = "INSERT INTO tenant_members (tenant_id, user_sub, role) VALUES ($1, $2, $3)"
_UPSERT_PRINCIPAL = (
    "INSERT INTO principals (issuer, sub, email, display_name) VALUES ($1, $2, $3, $4) "
    "ON CONFLICT (issuer, sub) DO UPDATE SET email = COALESCE(EXCLUDED.email, principals.email), "
    "display_name = COALESCE(EXCLUDED.display_name, principals.display_name), last_seen_at = now() "
    "RETURNING (xmax = 0) AS inserted"
)


async def is_member(conn, user_sub: str) -> bool:
    """Whether the person belongs to the caller's tenant (RLS scopes the lookup)."""
    return bool(await conn.fetchval(_IS_MEMBER, user_sub))


async def add_member(conn, tenant_id: uuid.UUID, user_sub: str, role: TenantRole) -> None:
    await conn.execute(_ADD_MEMBER, tenant_id, user_sub, role)


async def upsert_principal(conn, claims: Claims) -> None:
    # Claims may be absent from the access token; never overwrite a known value with nothing.
    # xmax is 0 only on a freshly inserted row, so it tells a first sign-in from a returning one.
    with tracer.start_as_current_span(AuthSpan.UPSERT_PRINCIPAL) as span:
        inserted = await conn.fetchval(_UPSERT_PRINCIPAL, ZITADEL_ISSUER, claims.subject, claims.email, claims.display_name)
        event = AuthEvent.SIGN_UP if inserted else AuthEvent.SIGN_IN
        span.set_attribute(AuthAttribute.EVENT, event)
    if inserted:
        auth_metrics.principals_created.add(1)
    auth_log.signed_in(claims.subject, event)
