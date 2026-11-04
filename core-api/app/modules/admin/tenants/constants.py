from app.core.authz import TenantStatus
from app.core.enums import IdentityStatus

# Status lives in Zitadel, so it can only be filtered on after enrichment —
# which means the status-filtered page has to be cut from the whole matching
# set, not from one LIMIT'd window of it. This caps how much of that set a
# single request will scan; the platform has tenants in the tens, and a
# listing that silently truncates is worse than one that says it did.
MAX_SCAN = 1000
# Owner lookups are one Zitadel call each (there is no bulk endpoint), so
# they run only for the rows actually being returned, a few at a time.
OWNER_CONCURRENCY = 8
ZITADEL_TIMEOUT = 10

# The statuses an org shows in the listing's tabs and the summary's counts.
ORG_STATUSES = (IdentityStatus.ACTIVE, IdentityStatus.SUSPENDED, IdentityStatus.UNKNOWN)

# pending_delete belongs to the owner's delete/restore flow, not to an operator's switch.
ADMIN_SETTABLE_STATUSES = (TenantStatus.ACTIVE, TenantStatus.SUSPENDED)

# A member whose Zitadel user is gone, or whose org Zitadel won't answer
# for. The listing degrades the same way (owner=null, status="unknown")
# rather than dropping the row: the membership is still real.
UNKNOWN_USER = {
    "display_name": None,
    "email": None,
    "login_name": None,
    "status": IdentityStatus.UNKNOWN,
}

NO_SUCH_TENANT = "no such tenant"
