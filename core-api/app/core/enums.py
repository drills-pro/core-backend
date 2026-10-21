from enum import StrEnum


class TaskType(StrEnum):
    """Mirrors the projects.task_type CHECK constraint. FR4 (02-org-team-project-model.md): set at
    creation, one of a fixed vocabulary, or the legacy escape hatch for pre-Phase-2 data."""

    IMAGE_CLASSIFICATION = "image_classification"
    OBJECT_DETECTION = "object_detection"
    TABULAR_CLASSIFICATION = "tabular_classification"
    TEXT_CLASSIFICATION = "text_classification"
    LEGACY_UNTYPED = "legacy_untyped"


class AuthResult(StrEnum):
    """Outcome of checking a bearer token: the `result` label on auth_token_verifications_total."""

    OK = "ok"
    NOT_BEARER = "not_bearer"
    EXPIRED = "expired"
    WRONG_AUDIENCE = "wrong_audience"
    WRONG_ISSUER = "wrong_issuer"
    BAD_SIGNATURE = "bad_signature"
    UNKNOWN_KEY = "unknown_key"  # kid not in Zitadel's JWKS
    JWKS_UNAVAILABLE = "jwks_unavailable"  # could not fetch Zitadel's JWKS at all
    MALFORMED = "malformed"


class UserinfoResult(StrEnum):
    """Outcome of a Zitadel userinfo lookup: the `result` label on zitadel_userinfo_total."""

    CACHED = "cached"
    FETCHED = "fetched"
    FAILED = "failed"
