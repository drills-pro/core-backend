from enum import StrEnum


class BlockstoreScheme(StrEnum):
    """URI scheme of lakeFS's storage namespaces; matches the blockstore type lakeFS runs with (`s3` also covers MinIO)."""

    S3 = "s3"
    GS = "gs"


class TaskType(StrEnum):
    """Hugging Face task slugs, verbatim. Mirrors the projects.task_type CHECK constraint. The whole
    taxonomy is accepted by the schema; core/task_catalog.py is what the platform can do today."""

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


class TrainPreset(StrEnum):
    """Model size of a training run (docs/trainer-plan.md T5). Mirrors the training_runs.preset CHECK constraint."""

    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"


class Split(StrEnum):
    """Values of the nullable `split` column. Datasets never invent one (plan decision 9)."""

    TRAIN = "train"
    VALIDATION = "validation"
    TEST = "test"


class ImportSource(StrEnum):
    """Where a dataset import's data comes from. Mirrors the dataset_imports_source_check constraint."""

    UPLOAD = "upload"
    HUGGINGFACE = "huggingface"


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




class PlanTier(StrEnum):
    """Mirrors the tenants.plan_tier CHECK constraint."""

    FREE = "free"
    PRO = "pro"
    ENTERPRISE = "enterprise"
