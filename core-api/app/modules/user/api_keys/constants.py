from enum import StrEnum


class AuditAction(StrEnum):
    CREATE = "api_key.create"
    REVOKE = "api_key.revoke"


class EntityType(StrEnum):
    API_KEY = "api_key"


class ProblemCode(StrEnum):
    UNKNOWN_PROJECT = "unknown_project"


UNKNOWN_PROJECT_DETAIL = "project_ids must all be active projects of this workspace"
