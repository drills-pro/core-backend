from enum import StrEnum

from app.core.enums import AdminAuditAction, CatalogSource, CatalogStatus

DEFAULT_SOURCE = CatalogSource.HF
STATUS_AUDIT_ACTION = {CatalogStatus.ENABLED: AdminAuditAction.MODEL_CATALOG_ENABLE, CatalogStatus.DISABLED: AdminAuditAction.MODEL_CATALOG_DISABLE}
SHA_PATTERN = r"^[0-9a-f]{40}$"  # mirrors model_catalog_revision_check: a commit, never a branch that moves
MAX_NAME_LENGTH = 200


class ProblemCode(StrEnum):
    TASK_NOT_SUPPORTED = "task_type_not_supported"
    LICENSE_NOT_ALLOWED = "license_not_allowed"
    MODEL_NOT_ADMITTED = "model_not_admitted"
    HUB_UNAVAILABLE = "hub_unavailable"
    ALREADY_IN_CATALOG = "already_in_catalog"
    IN_USE = "catalog_model_in_use"
