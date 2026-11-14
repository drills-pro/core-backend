from enum import StrEnum

from app.integrations import job_lifecycle


class DeploymentStatus(StrEnum):
    """Mirrors the deployments.status CHECK constraint."""

    DEPLOYING = "deploying"  # written only by retired legacy serving; a leftover row can still be re-deployed or deleted
    ACTIVE = "active"
    STOPPED = "stopped"
    FAILED = "failed"  # written only by retired legacy serving; a leftover row can be re-deployed or deleted
    DELETED = "deleted"


# Mirrored by the partial unique index deployments_one_live_per_run: the statuses that hold a training run.
LIVE_STATUSES = (DeploymentStatus.DEPLOYING, DeploymentStatus.ACTIVE, DeploymentStatus.STOPPED)
# What counts against the plan's max_active_deployments; stopped and deleted free the slot.
QUOTA_STATUSES = (DeploymentStatus.DEPLOYING, DeploymentStatus.ACTIVE)
# Mirrored by the partial unique index deployments_one_active_per_version: the statuses that hold a model version.
VERSION_LIVE_STATUSES = (DeploymentStatus.DEPLOYING, DeploymentStatus.ACTIVE)

_S = DeploymentStatus

# Every legal move of a deployment row; job_lifecycle.advance is the only status write (and writes the outbox event). A
# deployment is active as soon as it exists and nothing follows a Ray job, so no sweep uses this table: it has no in-flight
# status and no deadline. Active -> active is a 'main' re-deploy; stopped -> active is a start (a re-deploy refuses a stopped
# row on its own); 'deleted' is final, never re-deployed.
DEPLOYMENTS = job_lifecycle.JobTable(
    name="deployment",
    table="deployments",
    tenants_fn="inflight_deploy_tenant_ids",  # ponytail: never called (no sweep); JobTable requires one
    inflight=(),
    transitions={
        _S.DEPLOYING: frozenset({_S.ACTIVE, _S.DELETED}),
        _S.ACTIVE: frozenset({_S.ACTIVE, _S.STOPPED, _S.DELETED}),
        _S.STOPPED: frozenset({_S.ACTIVE, _S.DELETED}),
        _S.FAILED: frozenset({_S.ACTIVE, _S.DELETED}),
    },
    columns=frozenset({
        "training_run_id", "ray_job_id", "serving", "idempotency_key", "risk_acknowledged", "model_version_id", "predict_key_hash",
    }),
    completed_col=None,  # the table has no completed_at
    deadline=lambda row: None,
)
