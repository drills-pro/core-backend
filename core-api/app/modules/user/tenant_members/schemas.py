import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, StringConstraints

from app.core.authz import InviteRole, TenantRole
from app.modules.user.tenant_members.constants import EMAIL_MAX_LENGTH, EMAIL_PATTERN

Email = Annotated[str, StringConstraints(strip_whitespace=True, max_length=EMAIL_MAX_LENGTH, pattern=EMAIL_PATTERN)]


class RoleBody(BaseModel):
    role: TenantRole


class InvitationBody(BaseModel):
    email: Email
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
    email_sent: bool  # true: the link went to the invitee's mailbox, and only there
    token: str | None = None  # only when mail is off (dev): the inviter passes the link on by hand


class InviteContext(BaseModel):
    """What the invitation email names: the workspace and the person who sent it."""

    tenant_name: str
    inviter_name: str | None
    inviter_email: str | None


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
