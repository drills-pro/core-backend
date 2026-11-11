from enum import StrEnum


class AuditAction(StrEnum):
    CREATE = "api_key.create"
    REVOKE = "api_key.revoke"


class EntityType(StrEnum):
    API_KEY = "api_key"


class ProblemCode(StrEnum):
    UNKNOWN_MODEL = "unknown_model"


UNKNOWN_MODEL_DETAIL = "model_ids must all be models of this project"
