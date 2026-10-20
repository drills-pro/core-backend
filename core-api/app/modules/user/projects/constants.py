from enum import StrEnum


class ProjectStatus(StrEnum):
    """Mirrors the projects.status CHECK constraint."""

    ACTIVE = "active"
    ARCHIVED = "archived"
    PENDING_DELETE = "pending_delete"


class AuditAction(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    RESTORE = "restore"
    SET_MEMBER_ROLES = "set_member_roles"
    REMOVE_MEMBER = "remove_member"


class EntityType(StrEnum):
    PROJECT = "project"
    PROJECT_MEMBER = "project_member"


class ComputeTarget(StrEnum):
    CPU = "cpu"
    GPU = "gpu"


class DeployStrategy(StrEnum):
    MAIN = "main"
    LATEST = "latest"


class ProblemCode(StrEnum):
    GALLERY_PUBLISHED = "gallery_published"
    LAST_PROJECT_ADMIN = "last_project_admin"
    NOT_A_TENANT_MEMBER = "not_a_tenant_member"
    NOT_PENDING_DELETE = "not_pending_delete"


# Details of plain HTTPExceptions (no machine-readable code).
NOTHING_TO_UPDATE = "nothing to update"
NO_TEAM = "tenant has no team; create one first"
STATUS_NOT_SETTABLE = "status must be 'active' or 'archived'"
