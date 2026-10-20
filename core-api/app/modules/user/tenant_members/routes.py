"""Tenant membership HTTP surface (planning/04). Roles are the tenant's (owner, admin, member); project
bindings live in projects/members.py. Rules are in service.py."""
import uuid

from fastapi import APIRouter, Depends

from app.core.auth import Claims, get_profile_claims
from app.core.authz import Action, AuthzContext, public, require
from app.core.problem import not_found
from app.modules.user.tenant_members import service
from app.modules.user.tenant_members.schemas import (
    Invitation,
    InvitationAccepted,
    InvitationBody,
    InvitationCreated,
    Member,
    MemberRole,
    OwnershipOffer,
    OwnershipOfferBody,
    RoleBody,
)

router = APIRouter(prefix="/v1", tags=["tenant-members"])
ManageMembers = Depends(require(Action.TENANT_MEMBER_MANAGE))
AnyMember = Depends(require(Action.PROJECT_LIST))  # every tenant role holds it
TransferOwnership = Depends(require(Action.TENANT_TRANSFER_OWNERSHIP))


@router.get("/tenants/{tenant_id}/members", response_model=list[Member])
async def list_members(tenant_id: uuid.UUID, ctx: AuthzContext = ManageMembers):
    ctx.ensure_tenant(tenant_id)
    return await service.list_members(ctx)


@router.put("/tenants/{tenant_id}/members/{user_sub}", response_model=MemberRole)
async def set_member_role(tenant_id: uuid.UUID, user_sub: str, body: RoleBody, ctx: AuthzContext = ManageMembers):
    ctx.ensure_tenant(tenant_id)
    return await service.set_role(ctx, user_sub, body.role)


@router.delete("/tenants/{tenant_id}/members/{user_sub}", status_code=204)
async def remove_member(tenant_id: uuid.UUID, user_sub: str, ctx: AuthzContext = ManageMembers):
    ctx.ensure_tenant(tenant_id)
    await service.remove(ctx, user_sub)


@router.post("/tenants/{tenant_id}:leave", status_code=204)
async def leave_tenant(tenant_id: uuid.UUID, ctx: AuthzContext = AnyMember):
    ctx.ensure_tenant(tenant_id)
    await service.leave(ctx)


@router.post("/tenants/{tenant_id}/invitations", status_code=201, response_model=InvitationCreated)
async def create_invitation(tenant_id: uuid.UUID, body: InvitationBody, ctx: AuthzContext = ManageMembers):
    ctx.ensure_tenant(tenant_id)
    return await service.invite(ctx, body)


@router.get("/tenants/{tenant_id}/invitations", response_model=list[Invitation])
async def list_invitations(tenant_id: uuid.UUID, ctx: AuthzContext = ManageMembers):
    ctx.ensure_tenant(tenant_id)
    return await service.list_pending_invitations(ctx)


@router.post("/invitations/{token}:accept", dependencies=[Depends(public)], response_model=InvitationAccepted)  # the invitee is not a member yet
async def accept_invitation(token: str, claims: Claims = Depends(get_profile_claims)):
    if not claims.email_verified or not claims.email:
        raise not_found()
    return await service.accept(token, claims.email, claims.subject)


# One open ownership offer per tenant: an owner offers, a member accepts (planning/04).
@router.post("/tenants/{tenant_id}/ownership-offer", status_code=201, response_model=OwnershipOffer)
async def offer_ownership(tenant_id: uuid.UUID, body: OwnershipOfferBody, ctx: AuthzContext = TransferOwnership):
    ctx.ensure_tenant(tenant_id)
    return await service.offer_ownership(ctx, body.to_sub, body.step_down)


@router.get("/tenants/{tenant_id}/ownership-offer", response_model=OwnershipOffer)
async def get_ownership_offer(tenant_id: uuid.UUID, ctx: AuthzContext = AnyMember):
    ctx.ensure_tenant(tenant_id)
    return await service.get_offer(ctx)


@router.delete("/tenants/{tenant_id}/ownership-offer", status_code=204)
async def cancel_ownership_offer(tenant_id: uuid.UUID, ctx: AuthzContext = AnyMember):
    ctx.ensure_tenant(tenant_id)
    await service.cancel_offer(ctx)


@router.post("/tenants/{tenant_id}/ownership-offer:accept", response_model=MemberRole)
async def accept_ownership_offer(tenant_id: uuid.UUID, ctx: AuthzContext = AnyMember):
    ctx.ensure_tenant(tenant_id)
    return await service.accept_offer(ctx)
