"""Self-serve signup: a new Zitadel org + owner + tenant, as one saga (docs/signup-saga.md).

Zitadel first, Postgres last: a tenant row pointing at no org is the worse half-state, and the
tenant only becomes usable at the final step, so a login that races the saga gets 403
"organization not provisioned". Any failure deletes the org (which takes the owner and grants
with it) and the signups row. If the process dies mid-way instead, a retry with the same
Idempotency-Key resumes from the recorded state, and reap_stale_signups deletes what is left.
"""
import hashlib
import logging
import uuid

import httpx
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from app.core import auth, rate_limit
from app.core.db import get_pool
from app.core.tenant_conn import TenantConn
from app.integrations import zitadel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/auth", tags=["auth"])

STALE_AFTER = "15 minutes"
ZITADEL_TIMEOUT = 10


class SignupRequest(BaseModel):
    org_name: str
    first_name: str
    last_name: str
    email: str
    password: str


def _email_hash(email: str) -> str:
    return hashlib.sha256(email.lower().encode()).hexdigest()


def _client_ip(request: Request) -> str:
    # Traefik appends the peer it saw, so the last hop is the one the client cannot forge.
    # ponytail: assumes exactly one trusted proxy in front; revisit if a CDN is added.
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[-1].strip() or (request.client.host if request.client else "?")


async def _set(key: str, **fields) -> None:
    cols = ", ".join(f"{name} = ${i}" for i, name in enumerate(fields, start=2))
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(f"UPDATE signups SET {cols} WHERE key = $1", key, *fields.values())


async def _discard(client: httpx.AsyncClient, key: str, org_id: str | None) -> None:
    """Compensation. Best effort: failing to clean up must not mask the error that got us here."""
    if org_id:
        try:
            await zitadel.delete_org(client, org_id)
        except (httpx.HTTPError, OSError):
            logger.warning("left an orphan zitadel org %s; reaper will retry", org_id, exc_info=True)
            return  # keep the signups row: it is what lets the reaper find the org again
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM signups WHERE key = $1 AND state <> 'done'", key)


async def _create_tenant(name: str, org_id: str) -> str:
    # One transaction, scoped to the new tenant's own id so the RLS-protected teams insert is
    # allowed (tenants itself has no RLS).
    tenant_id = str(uuid.uuid4())
    async with TenantConn(tenant_id) as conn:
        await conn.execute(
            "INSERT INTO tenants (id, zitadel_org_id, name) VALUES ($1, $2, $3)", uuid.UUID(tenant_id), org_id, name
        )
        await conn.execute("INSERT INTO teams (tenant_id, name) VALUES ($1, 'Default')", uuid.UUID(tenant_id))
    return tenant_id


@router.post("/signup", status_code=201)
async def signup(body: SignupRequest, request: Request, idempotency_key: str = Header(...)):
    org_name, email = body.org_name.strip(), body.email.strip().lower()
    if not org_name:
        raise HTTPException(status_code=400, detail="org_name is required")
    if not (
        await rate_limit.check(f"signup:ip:{_client_ip(request)}", 5)
        and await rate_limit.check(f"signup:email:{email}", 3)
    ):
        raise HTTPException(status_code=429, detail="too many signup attempts")

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO signups (key, email, email_hash, org_name) VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (key) DO NOTHING",
            idempotency_key, email, _email_hash(email), org_name,
        )
        row = await conn.fetchrow("SELECT * FROM signups WHERE key = $1", idempotency_key)
    if row["email_hash"] != _email_hash(email):
        raise HTTPException(status_code=409, detail="Idempotency-Key was used for a different signup")
    if row["state"] == "done":
        return {"tenant_id": str(row["tenant_id"]), "org_id": row["org_id"]}

    state, org_id, user_id = row["state"], row["org_id"], row["user_id"]
    project_id = auth.project_id()
    async with httpx.AsyncClient(timeout=ZITADEL_TIMEOUT) as client:
        try:
            if state == "started":
                org_id, user_id = await zitadel.create_org_with_owner(
                    client, org_name, body.first_name, body.last_name, email, body.password
                )
                await _set(idempotency_key, state="org", org_id=org_id, user_id=user_id)
                state = "org"
            if state == "org":
                grant_id = await zitadel.create_project_grant(client, project_id, org_id)
                await zitadel.create_user_grant(client, org_id, user_id, project_id, grant_id)
                await _set(idempotency_key, state="granted")
            tenant_id = await _create_tenant(org_name, org_id)
            await _set(idempotency_key, state="done", tenant_id=uuid.UUID(tenant_id), email=None)
        except httpx.HTTPStatusError as error:
            await _discard(client, idempotency_key, org_id)
            raise _refused(error) from error
        except Exception as error:
            await _discard(client, idempotency_key, org_id)
            if isinstance(error, (httpx.HTTPError, OSError)):
                raise HTTPException(status_code=502, detail=f"zitadel unreachable: {error}") from error
            raise
    return {"tenant_id": tenant_id, "org_id": org_id}


def _refused(error: httpx.HTTPStatusError) -> HTTPException:
    try:
        detail = error.response.json().get("message", error.response.text)
    except ValueError:
        detail = error.response.text
    return HTTPException(status_code=409 if error.response.status_code < 500 else 502, detail=detail)


async def reap_stale_signups() -> None:
    """Poller: finish cleaning up signups that died mid-saga (crash, or a failed org delete)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        stale = await conn.fetch(
            f"SELECT key, org_id FROM signups WHERE state <> 'done' AND created_at < now() - interval '{STALE_AFTER}'"
        )
    async with httpx.AsyncClient(timeout=ZITADEL_TIMEOUT) as client:
        for row in stale:
            await _discard(client, row["key"], row["org_id"])
