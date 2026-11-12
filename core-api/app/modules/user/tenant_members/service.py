"""Business rules and queries for tenant membership; routes.py only translates HTTP to these calls."""
import hashlib
import secrets
from datetime import datetime, timezone
from http import HTTPStatus

import asyncpg

from app.core import audit
from app.core.auth import ZITADEL_ISSUER
from app.core.authz import Action, AuthzContext, InviteRole, TenantRole
from app.core.db import get_pool
from app.core.problem import Problem, not_found
from app.core.settings import settings
from app.core.tenant_conn import TenantConn
from app.integrations import mailer
from app.modules.user.tenant_members import invitation_mail
from app.modules.user.tenant_members.hooks import member_removed
from app.modules.user.tenant_members.invitation_mail import InvitationMail
from app.modules.user.tenant_members.constants import (
    INVITATION_TTL,
    OWNERSHIP_OFFER_TTL,
    AuditAction,
    EntityType,
    ProblemCode,
)
from app.modules.user.tenant_members.schemas import (
    Invitation,
    InvitationAccepted,
    InvitationBody,
    InvitationCreated,
    InviteContext,
    Member,
    MemberRole,
    OwnershipOffer,
)

# The tenant is visible under the caller's RLS; principals is not tenant data (no RLS).
_INVITE_CONTEXT = (
    "SELECT t.name AS tenant_name, p.display_name AS inviter_name, p.email AS inviter_email FROM tenants t "
    "LEFT JOIN principals p ON p.sub = $2 AND p.issuer = $3 WHERE t.id = $1"
)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _only_owner_touches_owner(ctx: AuthzContext, *roles: str) -> None:
    if ctx.tenant_role is not TenantRole.OWNER and TenantRole.OWNER in roles:
        raise Problem(403, ProblemCode.FORBIDDEN, "only an owner may grant or change the owner role", action=Action.TENANT_MEMBER_MANAGE.value)


def _last_owner() -> Problem:
    return Problem(409, ProblemCode.LAST_OWNER, "a tenant must keep at least one owner")


async def _lock_owners(conn, tenant_id: str) -> list[str]:
    # FOR UPDATE serialises two concurrent demotions, so they cannot both pass the last-owner check.
    rows = await conn.fetch("SELECT user_sub FROM tenant_members WHERE tenant_id = $1 AND role = $2 FOR UPDATE", tenant_id, TenantRole.OWNER)
    return [r["user_sub"] for r in rows]


async def _role_of(conn, tenant_id: str, user_sub: str) -> TenantRole:
    role = await conn.fetchval("SELECT role FROM tenant_members WHERE tenant_id = $1 AND user_sub = $2", tenant_id, user_sub)
    if role is None:
        raise not_found()
    return TenantRole(role)


async def list_members(ctx: AuthzContext) -> list[Member]:
    async with TenantConn(ctx.tenant_id) as conn:
        rows = await conn.fetch(
            "SELECT m.user_sub, m.role, m.created_at, p.email, p.display_name FROM tenant_members m "
            "LEFT JOIN principals p ON p.sub = m.user_sub AND p.issuer = $1 ORDER BY m.created_at, m.user_sub",
            ZITADEL_ISSUER,
        )
    return [Member.model_validate(dict(r)) for r in rows]


async def set_role(ctx: AuthzContext, user_sub: str, role: TenantRole) -> MemberRole:
    async with TenantConn(ctx.tenant_id) as conn:
        owners = await _lock_owners(conn, ctx.tenant_id)
        current = await _role_of(conn, ctx.tenant_id, user_sub)
        _only_owner_touches_owner(ctx, current, role)
        if role is TenantRole.OWNER and current is not TenantRole.OWNER:
            raise Problem(409, ProblemCode.USE_OWNERSHIP_OFFER, "ownership is granted by an offer the person accepts")
        if current is TenantRole.OWNER and role is not TenantRole.OWNER and owners == [user_sub]:
            raise _last_owner()
        await conn.execute("UPDATE tenant_members SET role = $3 WHERE tenant_id = $1 AND user_sub = $2", ctx.tenant_id, user_sub, role)
        await audit.record_for(conn, ctx, None, AuditAction.SET_ROLE, EntityType.TENANT_MEMBER, user_sub)
    return MemberRole(user_sub=user_sub, role=role)


async def _delete_member(conn, ctx: AuthzContext, user_sub: str, action: AuditAction) -> None:
    owners = await _lock_owners(conn, ctx.tenant_id)
    current = await _role_of(conn, ctx.tenant_id, user_sub)
    _only_owner_touches_owner(ctx, current)
    if owners == [user_sub]:
        raise _last_owner()
    # Bindings go too, so re-adding the person later does not resurrect old project access.
    await member_removed(conn, ctx.tenant_id, user_sub)
    await conn.execute("DELETE FROM tenant_members WHERE tenant_id = $1 AND user_sub = $2", ctx.tenant_id, user_sub)
    await audit.record_for(conn, ctx, None, action, EntityType.TENANT_MEMBER, user_sub)


async def remove(ctx: AuthzContext, user_sub: str) -> None:
    async with TenantConn(ctx.tenant_id) as conn:
        await _delete_member(conn, ctx, user_sub, AuditAction.REMOVE)


async def leave(ctx: AuthzContext) -> None:
    """Any member may leave; the sole owner must hand the tenant over first (409 last_owner)."""
    async with TenantConn(ctx.tenant_id) as conn:
        await _delete_member(conn, ctx, ctx.actor, AuditAction.LEAVE)


async def _invite_context(conn, ctx: AuthzContext) -> InviteContext:
    row = await conn.fetchrow(_INVITE_CONTEXT, ctx.tenant_id, ctx.actor, ZITADEL_ISSUER)
    return InviteContext.model_validate(dict(row))


async def _mail_invitation(conn, ctx: AuthzContext, email: str, role: InviteRole, token: str) -> None:
    about = await _invite_context(conn, ctx)
    inviter = about.inviter_name or about.inviter_email or ctx.actor
    try:
        await mailer.send(invitation_mail.build(
            InvitationMail(to=email, tenant_name=about.tenant_name, inviter=inviter, inviter_email=about.inviter_email,
                           role=role, token=token)
        ))
    except mailer.MailError:
        raise Problem(HTTPStatus.BAD_GATEWAY, ProblemCode.MAIL_NOT_SENT, "the invitation email could not be sent; try again") from None


async def invite(ctx: AuthzContext, body: InvitationBody) -> InvitationCreated:
    """The link goes to the invitee's mailbox only: accepting needs the token and a sign-in as that verified address.
    With mail off (dev, tests) the token comes back to the inviter instead."""
    token = secrets.token_urlsafe(32)
    mail_on = settings.mail.enabled
    async with TenantConn(ctx.tenant_id) as conn:
        row = await conn.fetchrow(
            "INSERT INTO tenant_invitations (tenant_id, email, role, token_hash, invited_by, expires_at) "
            "VALUES ($1, $2, $3, $4, $5, $6) RETURNING id, email, role, expires_at",
            ctx.tenant_id, body.email, body.role, hash_token(token), ctx.actor, datetime.now(timezone.utc) + INVITATION_TTL,
        )
        await audit.record_for(conn, ctx, None, AuditAction.INVITE, EntityType.TENANT_INVITATION, row["id"])
        if mail_on:
            # Sent before the commit: a mail that fails rolls the invitation back, so no live token exists that
            # nobody received, and the inviter simply tries again.
            await _mail_invitation(conn, ctx, row["email"], body.role, token)
    return InvitationCreated.model_validate({**dict(row), "email_sent": mail_on, "token": None if mail_on else token})


async def list_pending_invitations(ctx: AuthzContext) -> list[Invitation]:
    async with TenantConn(ctx.tenant_id) as conn:
        rows = await conn.fetch(
            "SELECT id, email, role, expires_at, created_at FROM tenant_invitations "
            "WHERE accepted_at IS NULL AND expires_at > now() ORDER BY created_at DESC"
        )
    return [Invitation.model_validate(dict(r)) for r in rows]


async def accept(token: str, email: str, user_sub: str) -> InvitationAccepted:
    """Every refusal is the same 404, so a probe learns nothing about which part was wrong."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        joined = await conn.fetchrow("SELECT * FROM accept_tenant_invitation($1, $2, $3)", hash_token(token), email, user_sub)
    if joined is None:
        raise not_found()
    out = InvitationAccepted.model_validate(dict(joined))
    async with TenantConn(str(out.tenant_id)) as conn:
        await audit.record(conn, tenant_id=str(out.tenant_id), project_id=None, actor=user_sub,
                           action=AuditAction.ACCEPT_INVITATION, entity_type=EntityType.TENANT_MEMBER, entity_id=user_sub)
    return out


async def _current_offer(conn, ctx: AuthzContext) -> OwnershipOffer:
    row = await conn.fetchrow(
        "SELECT from_sub, to_sub, step_down, created_at, expires_at FROM ownership_offers WHERE tenant_id = $1 AND expires_at > now()",
        ctx.tenant_id,
    )
    # Only the two people involved and the owners may know an offer exists.
    if row is None or not (ctx.actor in (row["from_sub"], row["to_sub"]) or ctx.tenant_role is TenantRole.OWNER):
        raise not_found()
    return OwnershipOffer.model_validate(dict(row))


async def get_offer(ctx: AuthzContext) -> OwnershipOffer:
    async with TenantConn(ctx.tenant_id) as conn:
        return await _current_offer(conn, ctx)


async def offer_ownership(ctx: AuthzContext, to_sub: str, step_down: bool) -> OwnershipOffer:
    async with TenantConn(ctx.tenant_id) as conn:
        if await _role_of(conn, ctx.tenant_id, to_sub) is TenantRole.OWNER:
            raise Problem(409, ProblemCode.ALREADY_OWNER, "that person is already an owner")
        await conn.execute("DELETE FROM ownership_offers WHERE tenant_id = $1 AND expires_at <= now()", ctx.tenant_id)
        try:
            row = await conn.fetchrow(
                "INSERT INTO ownership_offers (tenant_id, from_sub, to_sub, step_down, expires_at) VALUES ($1, $2, $3, $4, $5) "
                "RETURNING from_sub, to_sub, step_down, created_at, expires_at",
                ctx.tenant_id, ctx.actor, to_sub, step_down, datetime.now(timezone.utc) + OWNERSHIP_OFFER_TTL,
            )
        except asyncpg.UniqueViolationError:
            raise Problem(409, ProblemCode.OFFER_PENDING, "cancel the open ownership offer first") from None
        await audit.record_for(conn, ctx, None, AuditAction.OFFER_OWNERSHIP, EntityType.OWNERSHIP_OFFER, to_sub)
    return OwnershipOffer.model_validate(dict(row))


async def cancel_offer(ctx: AuthzContext) -> None:
    """The offering owner withdraws it, or the recipient declines."""
    async with TenantConn(ctx.tenant_id) as conn:
        offer = await _current_offer(conn, ctx)
        await conn.execute("DELETE FROM ownership_offers WHERE tenant_id = $1", ctx.tenant_id)
        await audit.record_for(conn, ctx, None, AuditAction.CANCEL_OWNERSHIP_OFFER, EntityType.OWNERSHIP_OFFER, offer.to_sub)


async def accept_offer(ctx: AuthzContext) -> MemberRole:
    async with TenantConn(ctx.tenant_id) as conn:
        # One statement claims the offer: only its recipient, only while it is open, only once.
        offer = await conn.fetchrow(
            "DELETE FROM ownership_offers WHERE tenant_id = $1 AND to_sub = $2 AND expires_at > now() RETURNING from_sub, step_down",
            ctx.tenant_id, ctx.actor,
        )
        if offer is None:
            raise not_found()
        owners = await _lock_owners(conn, ctx.tenant_id)
        if offer["from_sub"] not in owners:
            raise Problem(409, ProblemCode.OFFER_STALE, "the offering owner is no longer an owner")
        await conn.execute("UPDATE tenant_members SET role = $3 WHERE tenant_id = $1 AND user_sub = $2", ctx.tenant_id, ctx.actor, TenantRole.OWNER)
        if offer["step_down"]:
            await conn.execute("UPDATE tenant_members SET role = $3 WHERE tenant_id = $1 AND user_sub = $2", ctx.tenant_id, offer["from_sub"], TenantRole.ADMIN)
        await audit.record_for(conn, ctx, None, AuditAction.ACCEPT_OWNERSHIP, EntityType.OWNERSHIP_OFFER, ctx.actor)
    return MemberRole(user_sub=ctx.actor, role=TenantRole.OWNER)
