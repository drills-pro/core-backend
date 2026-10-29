from enum import StrEnum

from app.core.problem import Problem


class BlockstoreScheme(StrEnum):
    """URI scheme of lakeFS's storage namespaces; matches the blockstore type lakeFS runs with (`s3` also covers MinIO)."""

    S3 = "s3"
    GS = "gs"


class TaskType(StrEnum):
    """Hugging Face task slugs, verbatim. Mirrors the projects.task_type CHECK constraint. The whole
    taxonomy is accepted by the schema; SUPPORTED_TASK_TYPES is what the platform can do today."""

    # image
    IMAGE_CLASSIFICATION = "image-classification"
    OBJECT_DETECTION = "object-detection"
    IMAGE_SEGMENTATION = "image-segmentation"
    # tabular
    TABULAR_CLASSIFICATION = "tabular-classification"
    TABULAR_REGRESSION = "tabular-regression"
    # text
    TEXT_CLASSIFICATION = "text-classification"
    TOKEN_CLASSIFICATION = "token-classification"
    QUESTION_ANSWERING = "question-answering"
    SUMMARIZATION = "summarization"
    TRANSLATION = "translation"
    TEXT_GENERATION = "text-generation"
    # audio
    AUDIO_CLASSIFICATION = "audio-classification"
    AUTOMATIC_SPEECH_RECOGNITION = "automatic-speech-recognition"
    TEXT_TO_SPEECH = "text-to-speech"


SUPPORTED_TASK_TYPES = frozenset(
    {
        TaskType.IMAGE_CLASSIFICATION,
        TaskType.OBJECT_DETECTION,
        TaskType.TABULAR_CLASSIFICATION,
        TaskType.TABULAR_REGRESSION,
    }
)


TABULAR_TASKS = frozenset({TaskType.TABULAR_CLASSIFICATION, TaskType.TABULAR_REGRESSION})


class ImportSource(StrEnum):
    """Where a dataset import's data comes from. Mirrors the dataset_imports_source_check constraint."""

    UPLOAD = "upload"
    HUGGINGFACE = "huggingface"


def require_supported_task_type(task_type: TaskType) -> None:
    if task_type not in SUPPORTED_TASK_TYPES:
        raise Problem(422, "task_type_not_supported", f"task type '{task_type}' is not supported yet")


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
    """Outcome of a Zitadel userinfo lookup: the `result` label on auth_zitadel_userinfo_total."""

    CACHED = "cached"
    FETCHED = "fetched"
    FAILED = "failed"


class AuthEvent(StrEnum):
    """The `auth.event` attribute on the sign-in log records core/auth_log.py ships to Loki."""

    SIGN_IN = "sign_in"  # a known user's session reached /v1/me
    SIGN_UP = "sign_up"  # the same, for an identity seen for the first time
    TOKEN_REJECTED = "token_rejected"


class AuthAttribute(StrEnum):
    """Attribute keys shared by the auth log records (Loki) and spans (Tempo), so one name finds both.
    Loki turns the dots into underscores."""

    EVENT = "auth.event"  # AuthEvent
    RESULT = "auth.result"  # AuthResult
    USERINFO_RESULT = "auth.userinfo.result"  # UserinfoResult
    USER = "user.id"  # Zitadel's sub, an opaque id; never the email


class AuthSpan(StrEnum):
    """Span names for the steps of authenticating a request, under the FastAPI request span."""

    VERIFY_TOKEN = "auth.verify_token"
    JWKS_FETCH = "auth.jwks.fetch"  # only on a JWKS cache miss
    USERINFO = "auth.userinfo"
    UPSERT_PRINCIPAL = "auth.upsert_principal"


