from enum import StrEnum


class ProjectStatus(StrEnum):
    """Mirrors the projects.status CHECK constraint."""

    ACTIVE = "active"
    ARCHIVED = "archived"
    PENDING_DELETE = "pending_delete"


class Sharing(StrEnum):
    """Mirrors the projects.sharing CHECK constraint: what the project shows in the public gallery."""

    PRIVATE = "private"
    GALLERY = "gallery"  # name, task type and the versions' metadata
    GALLERY_DOWNLOADS = "gallery_downloads"  # plus model artifacts and published dataset versions


class AuditAction(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    RESTORE = "restore"
    SET_MEMBER_ROLES = "set_member_roles"
    SET_SHARING = "set_sharing"
    REMOVE_MEMBER = "remove_member"


class EntityType(StrEnum):
    PROJECT = "project"
    PROJECT_MEMBER = "project_member"


class DeployStrategy(StrEnum):
    MAIN = "main"
    LATEST = "latest"


class ProblemCode(StrEnum):
    LAST_PROJECT_ADMIN = "last_project_admin"
    NOT_A_TENANT_MEMBER = "not_a_tenant_member"
    NOT_PENDING_DELETE = "not_pending_delete"
    NO_TEAM = "no_team"
    NOTHING_TO_UPDATE = "nothing_to_update"
    STATUS_NOT_SETTABLE = "status_not_settable"


NOTHING_TO_UPDATE = "nothing to update"
NO_TEAM = "tenant has no team; create one first"
STATUS_NOT_SETTABLE = "status must be 'active' or 'archived'"
