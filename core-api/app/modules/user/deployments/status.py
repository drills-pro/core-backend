from enum import StrEnum


class DeploymentStatus(StrEnum):
    """Mirrors the deployments.status CHECK constraint."""

    DEPLOYING = "deploying"
    ACTIVE = "active"
    FAILED = "failed"
    DELETED = "deleted"
