from enum import StrEnum

TENANT_KEY_MAX_LENGTH = 128  # the tenant_create_keys.key column

# Tenant creation is capped per user and per client IP, per minute.
TENANT_CREATE_USER_LIMIT = 3
TENANT_CREATE_IP_LIMIT = 5
TENANT_CREATE_USER_RATE_KEY = "tenant:sub:{}"
TENANT_CREATE_IP_RATE_KEY = "tenant:ip:{}"

# Name of the UNIQUE constraint on tenants.slug, to tell a taken slug from a replayed idempotency key.
TENANT_SLUG_CONSTRAINT = "tenants_slug_key"

# Email verification: Zitadel mails the link and checks the code. The link opens Zitadel's own login UI (k8s
# infra/zitadel, login v2), whose /verify page marks the email verified; Zitadel fills in the {{.X}} placeholders.
ZITADEL_VERIFY_URL_TEMPLATE = "/ui/v2/login/verify?userId={{.UserID}}&code={{.Code}}&organization={{.OrgID}}"
# One mail per user per minute. The SPA polls the status every few seconds while it waits.
VERIFY_SEND_USER_LIMIT = 1
VERIFY_SEND_USER_RATE_KEY = "verify-send:sub:{}"
VERIFY_STATUS_USER_LIMIT = 30
VERIFY_STATUS_USER_RATE_KEY = "verify-status:sub:{}"


class ProblemCode(StrEnum):
    EMAIL_NOT_VERIFIED = "email_not_verified"
    TOO_MANY_ATTEMPTS = "too_many_attempts"
    SLUG_TAKEN = "slug_taken"
    REQUEST_IN_PROGRESS = "request_in_progress"  # the same Idempotency-Key is still being served
    EMAIL_ALREADY_VERIFIED = "email_already_verified"  # the caller's token predates the verification: sign in again
    NO_EMAIL = "no_email"  # the identity has no address to verify
    IDENTITY_UNAVAILABLE = "identity_unavailable"  # Zitadel did not answer
