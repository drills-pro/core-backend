import uuid
from datetime import datetime

from pydantic import BaseModel

from app.core.authz import InviteRole, TenantRole


class RoleBody(BaseModel):
    role: TenantRole


class InvitationBody(BaseModel):
    email: str
    role: InviteRole


class Member(BaseModel):
    user_sub: str
    role: TenantRole
    created_at: datetime
    email: str | None = None
    display_name: str | None = None


class MemberRole(BaseModel):
    user_sub: str
    role: TenantRole


class Invitation(BaseModel):
    id: uuid.UUID
    email: str
    role: InviteRole
    expires_at: datetime
    created_at: datetime | None = None


class InvitationCreated(Invitation):
    token: str  # the one time the plaintext exists server-side


class InvitationAccepted(BaseModel):
    tenant_id: uuid.UUID
    role: TenantRole


class OwnershipOfferBody(BaseModel):
    to_sub: str
    step_down: bool = False  # the offering owner becomes an admin once the offer is accepted


class OwnershipOffer(BaseModel):
    from_sub: str
    to_sub: str
    step_down: bool
    created_at: datetime
    expires_at: datetime
