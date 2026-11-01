TENANT_KEY_MAX_LENGTH = 128  # the tenant_create_keys.key column

# Tenant creation is capped per user and per client IP, per minute.
TENANT_CREATE_USER_LIMIT = 3
TENANT_CREATE_IP_LIMIT = 5
TENANT_CREATE_USER_RATE_KEY = "tenant:sub:{}"
TENANT_CREATE_IP_RATE_KEY = "tenant:ip:{}"

# Name of the UNIQUE constraint on tenants.slug, to tell a taken slug from a replayed idempotency key.
TENANT_SLUG_CONSTRAINT = "tenants_slug_key"
