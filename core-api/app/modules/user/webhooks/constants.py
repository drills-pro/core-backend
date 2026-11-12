from enum import StrEnum

SIGNING_SECRET_FILE = "webhooks"  # app/core/secret_files name; its "signing_key" is the master the endpoint secrets derive from
SIGNING_KEY_FIELD = "signing_key"

SIGNATURE_HEADER = "X-Drills-Signature"  # v1=<hex hmac-sha256 of "<timestamp>.<body>" with the endpoint secret>
TIMESTAMP_HEADER = "X-Drills-Timestamp"
EVENT_ID_HEADER = "X-Drills-Event-Id"  # the event's seq: the receiver's idempotency key (delivery is at least once)
SIGNATURE_VERSION = "v1"

DELIVERY_TIMEOUT_SECONDS = 10
DELIVERY_BATCH = 50  # events one endpoint is sent per tick
BACKOFF_BASE_SECONDS = 30
BACKOFF_FACTOR = 2
BACKOFF_MAX_SECONDS = 3600
DISABLE_AFTER_FAILURES = 20  # consecutive failed ticks; the endpoint is then disabled and must be created again
URL_MAX_LENGTH = 2048
BLOCKED_HOST_SUFFIXES = (".local", ".internal", ".svc", ".cluster.local", ".localhost")


class AuditAction(StrEnum):
    CREATE = "webhook.create"
    DELETE = "webhook.delete"


class EntityType(StrEnum):
    WEBHOOK = "webhook"


class ProblemCode(StrEnum):
    UNAVAILABLE = "webhooks_unavailable"
    BAD_URL = "invalid_webhook_url"


UNAVAILABLE_DETAIL = "webhook delivery is not configured on this platform"
