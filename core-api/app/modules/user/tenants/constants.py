from enum import StrEnum


class AuditAction(StrEnum):
    RENAME = "rename"
    DELETE = "delete"
    RESTORE = "restore"


class EntityType(StrEnum):
    TENANT = "tenant"


class ProblemCode(StrEnum):
    CONFIRMATION_MISMATCH = "confirmation_mismatch"
    NOT_PENDING_DELETE = "not_pending_delete"
