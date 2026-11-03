from enum import StrEnum


class AuditAction(StrEnum):
    CREATE = "api_key.create"
    REVOKE = "api_key.revoke"


class EntityType(StrEnum):
    API_KEY = "api_key"
