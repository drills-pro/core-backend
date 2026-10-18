import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, Field

from app.core import audit
from app.core.auth import Claims, get_claims, get_profile_claims
from app.core.authz import Action, AuthzContext, Role, TenantRole, require
from app.core.problem import Problem, not_found
from app.core.settings import settings
from app.core.tenant_conn import TenantConn
from app.modules.user.projects.routes import ProjectStatus

router = APIRouter(prefix="/v1/projects/{project_id}", tags=["project-members"])


class RoleSet(BaseModel):
    roles: list[Role] = Field(min_length=1)


def _hash_token(token: str) -> str:
    # Only this hash is stored (init migration): a database read never yields a usable invite link.
    return hashlib.sha256(token.encode()).hexdigest()


async def _keep_an_admin(conn, ctx: AuthzContext, project_id: uuid.UUID, user_sub: str) -> None:
    """Refuse to strip the last project_admin; tenant owner/admin can always recover the project, so they may."""
    if ctx.tenant_role in (TenantRole.OWNER, TenantRole.ADMIN):
        return
    # ponytail: unlocked check, a concurrent pair of removals could still leave none; owner/admin recover it.
    if await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM project_members WHERE project_id = $1 AND user_sub = $2 AND role = $3) "
        "AND NOT EXISTS (SELECT 1 FROM project_members WHERE project_id = $1 AND user_sub <> $2 AND role = $3)",
        project_id,
        user_sub,
        Role.PROJECT_ADMIN,
    ):
        raise Problem(409, "last_project_admin", "a project needs at least one project_admin")


@router.get("/members")
async def list_members(project_id: uuid.UUID, ctx: AuthzContext = Depends(require(Action.PROJECT_READ))):
    """One entry per person with their bound roles (`internal` access is not listed: it is not a binding)."""
    async with TenantConn(ctx.tenant_id) as conn:
        rows = await conn.fetch(
            "SELECT user_sub, array_agg(role ORDER BY role) AS roles, min(created_at) AS created_at "
            "FROM project_members WHERE project_id = $1 GROUP BY user_sub ORDER BY min(created_at)",
            project_id,
        )
    return [dict(r) for r in rows]


@router.put("/members/{user_sub}")
async def set_member_roles(
    project_id: uuid.UUID,
    user_sub: str,
    body: RoleSet,
    ctx: AuthzContext = Depends(require(Action.PROJECT_MEMBER_MANAGE)),
):
    """Replace the person's bindings with exactly these roles. Targets must be tenant members."""
    roles = sorted({r.value for r in body.roles})
    async with TenantConn(ctx.tenant_id) as conn:
        if not await conn.fetchval("SELECT 1 FROM tenant_members WHERE user_sub = $1", user_sub):
            raise Problem(400, "not_a_tenant_member", "only tenant members can be bound to a project")
        if Role.PROJECT_ADMIN not in roles:
            await _keep_an_admin(conn, ctx, project_id, user_sub)
        await conn.execute(
            "DELETE FROM project_members WHERE project_id = $1 AND user_sub = $2 AND role <> ALL($3::text[])",
            project_id,
            user_sub,
            roles,
        )
        await conn.execute(
            "INSERT INTO project_members (tenant_id, project_id, user_sub, role) "
            "SELECT $1, $2, $3, r FROM unnest($4::text[]) r ON CONFLICT DO NOTHING",
            ctx.tenant_id,
            project_id,
            user_sub,
            roles,
        )
        await audit.record(
            conn,
            tenant_id=ctx.tenant_id,
            project_id=project_id,
            actor=ctx.actor,
            action="set_member_roles",
            entity_type="project_member",
            entity_id=user_sub,
        )
    return {"project_id": str(project_id), "user_sub": user_sub, "roles": roles}


@router.delete("/members/{user_sub}", status_code=204)
async def remove_member(
    project_id: uuid.UUID,
    user_sub: str,
    ctx: AuthzContext = Depends(require(Action.PROJECT_MEMBER_MANAGE)),
):
    """Remove all of the person's bindings."""
    async with TenantConn(ctx.tenant_id) as conn:
        await _keep_an_admin(conn, ctx, project_id, user_sub)
        result = await conn.execute(
            "DELETE FROM project_members WHERE project_id = $1 AND user_sub = $2", project_id, user_sub
        )
        if result == "DELETE 0":
            raise not_found()
        await audit.record(
            conn,
            tenant_id=ctx.tenant_id,
            project_id=project_id,
            actor=ctx.actor,
            action="remove_member",
            entity_type="project_member",
            entity_id=user_sub,
        )


@router.post("/invitations", status_code=201)
async def create_invitation(
    project_id: uuid.UUID,
    email: str,
    role: Role,
    ctx: AuthzContext = Depends(require(Action.PROJECT_MEMBER_MANAGE)),
):
    # FR5: invite by email, role pre-assigned, 7-day expiry.
    # ponytail: returns the raw token instead of sending an email — no
    # email-delivery service exists yet (SES/Postmark/etc. wiring is a
    # separate, non-trivial piece). Swap the return for a "sent" ack once
    # that's wired; the invitation row/token model doesn't change.
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(days=7)
    async with TenantConn(ctx.tenant_id) as conn:
        row = await conn.fetchrow(
            "INSERT INTO project_invitations "
            "(tenant_id, project_id, email, role, token_hash, invited_by, expires_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7) "
            "RETURNING id, email, role, expires_at",
            ctx.tenant_id,
            project_id,
            email,
            role,
            _hash_token(token),
            ctx.actor,
            expires_at,
        )
        await audit.record(
            conn,
            tenant_id=ctx.tenant_id,
            project_id=project_id,
            actor=ctx.actor,
            action="invite",
            entity_type="project_invitation",
            entity_id=row["id"],
        )
    # The one and only time the plaintext token exists server-side.
    return {**dict(row), "token": token}


async def _verified_email(
    authorization: str | None = Header(default=None),
    x_user_email: str | None = Header(default=None),
) -> str | None:
    """The caller's verified email, or None. Dev-header mode (no token) trusts X-User-Email."""
    if authorization is None:
        return x_user_email if settings.auth.dev_headers else None
    claims = await get_profile_claims(await get_claims(authorization))
    return claims.email if claims.email_verified else None


# PROJECT_LIST: the invitee holds no binding yet, so a project action would 404 on a private
# project. Any tenant member may present a token; the token AND a matching verified email are the capability.
@router.post("/invitations/{token}/accept")
async def accept_invitation(
    project_id: uuid.UUID,
    token: str,
    ctx: AuthzContext = Depends(require(Action.PROJECT_LIST)),
    email: str | None = Depends(_verified_email),
):
    # Every refusal is the same 404, so a probe learns nothing about which part of the token was wrong.
    if email is None:
        raise not_found()
    async with TenantConn(ctx.tenant_id) as conn:
        # One statement claims the token: of two concurrent accepts only one gets a row back.
        invite = await conn.fetchrow(
            "UPDATE project_invitations i SET accepted_at = now() FROM projects p "
            "WHERE i.project_id = $1 AND i.token_hash = $2 AND i.accepted_at IS NULL AND i.expires_at > now() "
            "AND lower(i.email) = lower($3) AND p.id = i.project_id AND p.status <> $4 "
            "RETURNING i.id, i.role",
            project_id,
            _hash_token(token),
            email,
            ProjectStatus.PENDING_DELETE,
        )
        if invite is None:
            raise not_found()
        await conn.execute(
            "INSERT INTO project_members (tenant_id, project_id, user_sub, role) "
            "VALUES ($1, $2, $3, $4) ON CONFLICT DO NOTHING",
            ctx.tenant_id,
            project_id,
            ctx.actor,
            Role(invite["role"]),
        )
        await audit.record(
            conn,
            tenant_id=ctx.tenant_id,
            project_id=project_id,
            actor=ctx.actor,
            action="accept_invitation",
            entity_type="project_invitation",
            entity_id=invite["id"],
        )
    return {"project_id": str(project_id), "user_sub": ctx.actor, "role": invite["role"]}


@router.get("/audit-log")
async def get_audit_log(
    project_id: uuid.UUID,
    ctx: AuthzContext = Depends(require(Action.AUDIT_READ)),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    # FR3 + NFR1: paginated project audit trail.
    async with TenantConn(ctx.tenant_id) as conn:
        rows = await conn.fetch(
            "SELECT id, actor, action, entity_type, entity_id, created_at FROM audit_log "
            "WHERE project_id = $1 ORDER BY created_at DESC LIMIT $2 OFFSET $3",
            project_id,
            limit,
            offset,
        )
    return [dict(r) for r in rows]
