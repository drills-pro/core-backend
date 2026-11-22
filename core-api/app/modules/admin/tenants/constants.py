from enum import StrEnum

from app.core.enums import IdentityStatus

# The statuses an org shows in the listing's tabs and the summary's counts.
ORG_STATUSES = (IdentityStatus.ACTIVE, IdentityStatus.SUSPENDED, IdentityStatus.UNKNOWN)


class ProblemCode(StrEnum):
    NAME_REQUIRED = "name_required"
    INVALID_PLAN_TIER = "invalid_plan_tier"
    ORG_ALREADY_MAPPED = "org_already_mapped"
    ZITADEL_REFUSED = "zitadel_refused"
    ZITADEL_UNREACHABLE = "zitadel_unreachable"
    NO_TEAM = "no_team"
    STATUS_NOT_SETTABLE = "status_not_settable"
