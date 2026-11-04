from enum import StrEnum

from app.core.authz import TenantStatus
from app.core.enums import IdentityStatus

# Status lives in Zitadel, so it can only be filtered on after enrichment —
# which means the status-filtered page has to be cut from the whole matching
# set, not from one LIMIT'd window of it. This caps how much of that set a
# single request will scan; the platform has tenants in the tens, and a
# listing that silently truncates is worse than one that says it did.
MAX_SCAN = 1000

# The statuses an org shows in the listing's tabs and the summary's counts.
ORG_STATUSES = (IdentityStatus.ACTIVE, IdentityStatus.SUSPENDED, IdentityStatus.UNKNOWN)

# pending_delete belongs to the owner's delete/restore flow, not to an operator's switch.
ADMIN_SETTABLE_STATUSES = (TenantStatus.ACTIVE, TenantStatus.SUSPENDED)


class ProblemCode(StrEnum):
    NAME_REQUIRED = "name_required"
    INVALID_PLAN_TIER = "invalid_plan_tier"
    ORG_ALREADY_MAPPED = "org_already_mapped"
    ZITADEL_REFUSED = "zitadel_refused"
    ZITADEL_UNREACHABLE = "zitadel_unreachable"
    NO_TEAM = "no_team"
    STATUS_NOT_SETTABLE = "status_not_settable"
