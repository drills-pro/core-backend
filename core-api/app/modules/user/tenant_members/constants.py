from datetime import timedelta
from enum import StrEnum

INVITATION_TTL = timedelta(days=7)
INVITE_PATH = "/invite/{token}"  # the customer SPA's accept page (customer-frontend src/modules/members/routes.tsx)
EMAIL_MAX_LENGTH = 254  # RFC 5321 path limit
EMAIL_PATTERN = r"^[^\s@]+@[^\s@]+\.[^\s@]+$"  # the SPA's check; the mailbox proves the rest
OWNERSHIP_OFFER_TTL = timedelta(days=7)


class OfferStatus(StrEnum):
    """Derived from expires_at, never stored: an expired offer stays readable until it is cancelled or replaced."""

    PENDING = "pending"
    EXPIRED = "expired"


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
    MAIL_NOT_SENT = "mail_not_sent"
