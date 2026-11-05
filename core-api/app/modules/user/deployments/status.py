from enum import StrEnum


class DeploymentStatus(StrEnum):
    """Mirrors the deployments.status CHECK constraint."""

    DEPLOYING = "deploying"
    ACTIVE = "active"
    STOPPED = "stopped"
    FAILED = "failed"
    DELETED = "deleted"


# Mirrored by the partial unique index deployments_one_live_per_run: the statuses that hold a training run.
LIVE_STATUSES = (DeploymentStatus.DEPLOYING, DeploymentStatus.ACTIVE, DeploymentStatus.STOPPED)
# What counts against the plan's max_active_deployments; stopped and deleted free the slot.
QUOTA_STATUSES = (DeploymentStatus.DEPLOYING, DeploymentStatus.ACTIVE)
