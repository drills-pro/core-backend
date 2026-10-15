import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core import audit
from app.core.tenancy import get_actor, get_tenant_id
from app.core.tenant_conn import TenantConn

router = APIRouter(prefix="/v1/projects/{project_id}", tags=["project-members"])

VALID_ROLES = ("project_editor", "project_viewer")


def _hash_token(token: str) -> str:
    # Only this hash is stored (migration 0034): a database read never yields a usable invite link.
    return hashlib.sha256(token.encode()).hexdigest()


@router.get("/members")
async def list_members(project_id: uuid.UUID, tenant_id: str = Depends(get_tenant_id)):
    async with TenantConn(tenant_id) as conn:
        rows = await conn.fetch(
            "SELECT user_id, role, created_at FROM project_members "
            "WHERE project_id = $1 ORDER BY created_at",
            project_id,
        )
    return [dict(r) for r in rows]


@router.put("/members/{user_id}")
async def set_member_role(
    project_id: uuid.UUID,
    user_id: str,
    role: str,
    tenant_id: str = Depends(get_tenant_id),
    actor: str = Depends(get_actor),
):
    # FR2: project-level role, independent of org-level role.
    if role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"role must be one of {VALID_ROLES}")
    async with TenantConn(tenant_id) as conn:
        await conn.execute(
            "INSERT INTO project_members (tenant_id, project_id, user_id, role) "
            "VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (project_id, user_id) DO UPDATE SET role = EXCLUDED.role",
            tenant_id,
            project_id,
            user_id,
            role,
        )
        await audit.record(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            actor=actor,
            action="set_member_role",
            entity_type="project_member",
            entity_id=user_id,
        )
    return {"project_id": str(project_id), "user_id": user_id, "role": role}


@router.delete("/members/{user_id}", status_code=204)
async def remove_member(
    project_id: uuid.UUID,
    user_id: str,
    tenant_id: str = Depends(get_tenant_id),
    actor: str = Depends(get_actor),
):
    async with TenantConn(tenant_id) as conn:
        result = await conn.execute(
            "DELETE FROM project_members WHERE project_id = $1 AND user_id = $2",
            project_id,
            user_id,
        )
        if result == "DELETE 0":
            raise HTTPException(status_code=404, detail="member not found")
        await audit.record(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            actor=actor,
            action="remove_member",
            entity_type="project_member",
            entity_id=user_id,
        )


@router.post("/invitations", status_code=201)
async def create_invitation(
    project_id: uuid.UUID,
    email: str,
    role: str,
    tenant_id: str = Depends(get_tenant_id),
    actor: str = Depends(get_actor),
):
    # FR5: invite by email, role pre-assigned, 7-day expiry.
    # ponytail: returns the raw token instead of sending an email — no
    # email-delivery service exists yet (SES/Postmark/etc. wiring is a
    # separate, non-trivial piece). Swap the return for a "sent" ack once
    # that's wired; the invitation row/token model doesn't change.
    if role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"role must be one of {VALID_ROLES}")
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(days=7)
    async with TenantConn(tenant_id) as conn:
        row = await conn.fetchrow(
            "INSERT INTO project_invitations "
            "(tenant_id, project_id, email, role, token_hash, invited_by, expires_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7) "
            "RETURNING id, email, role, expires_at",
            tenant_id,
            project_id,
            email,
            role,
            _hash_token(token),
            actor,
            expires_at,
        )
        await audit.record(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            actor=actor,
            action="invite",
            entity_type="project_invitation",
            entity_id=row["id"],
        )
    # The one and only time the plaintext token exists server-side.
    return {**dict(row), "token": token}


@router.post("/invitations/{token}/accept")
async def accept_invitation(
    project_id: uuid.UUID,
    token: str,
    tenant_id: str = Depends(get_tenant_id),
    actor: str = Depends(get_actor),
):
    async with TenantConn(tenant_id) as conn:
        invite = await conn.fetchrow(
            "SELECT id, role, expires_at, accepted_at FROM project_invitations "
            "WHERE project_id = $1 AND token_hash = $2",
            project_id,
            _hash_token(token),
        )
        if invite is None:
            raise HTTPException(status_code=404, detail="invitation not found")
        if invite["accepted_at"] is not None:
            raise HTTPException(status_code=409, detail="invitation already accepted")
        if invite["expires_at"] < datetime.now(timezone.utc):
            raise HTTPException(status_code=410, detail="invitation expired")

        await conn.execute(
            "INSERT INTO project_members (tenant_id, project_id, user_id, role) "
            "VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (project_id, user_id) DO UPDATE SET role = EXCLUDED.role",
            tenant_id,
            project_id,
            actor,
            invite["role"],
        )
        await conn.execute(
            "UPDATE project_invitations SET accepted_at = now(), email = NULL WHERE id = $1", invite["id"]
        )
        await audit.record(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            actor=actor,
            action="accept_invitation",
            entity_type="project_invitation",
            entity_id=invite["id"],
        )
    return {"project_id": str(project_id), "user_id": actor, "role": invite["role"]}


@router.get("/audit-log")
async def get_audit_log(
    project_id: uuid.UUID,
    tenant_id: str = Depends(get_tenant_id),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    # FR3 + NFR1: paginated project audit trail.
    async with TenantConn(tenant_id) as conn:
        rows = await conn.fetch(
            "SELECT id, actor, action, entity_type, entity_id, created_at FROM audit_log "
            "WHERE project_id = $1 ORDER BY created_at DESC LIMIT $2 OFFSET $3",
            project_id,
            limit,
            offset,
        )
    return [dict(r) for r in rows]
