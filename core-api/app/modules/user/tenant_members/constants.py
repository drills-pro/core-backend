from datetime import timedelta
from enum import StrEnum

INVITATION_TTL = timedelta(days=7)
OWNERSHIP_OFFER_TTL = timedelta(days=7)


class AuditAction(StrEnum):
    SET_ROLE = "set_role"
    REMOVE = "remove"
    INVITE = "invite"
    ACCEPT_INVITATION = "accept_invitation"
    LEAVE = "leave"
    OFFER_OWNERSHIP = "offer_ownership"
    CANCEL_OWNERSHIP_OFFER = "cancel_ownership_offer"
    ACCEPT_OWNERSHIP = "accept_ownership"


class EntityType(StrEnum):
    TENANT_MEMBER = "tenant_member"
    TENANT_INVITATION = "tenant_invitation"
    OWNERSHIP_OFFER = "ownership_offer"


class ProblemCode(StrEnum):
    FORBIDDEN = "forbidden"
    LAST_OWNER = "last_owner"
    USE_OWNERSHIP_OFFER = "use_ownership_offer"
    ALREADY_OWNER = "already_owner"
    OFFER_PENDING = "ownership_offer_pending"
    OFFER_STALE = "ownership_offer_stale"
