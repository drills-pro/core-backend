"""What deployments offers other contexts. Takes the caller's connection, so it runs under the same RLS."""
import uuid

from app.modules.user.deployments.schemas import RunDeployment

_RUN_DEPLOYMENT = (
    "SELECT id, status, route_prefix FROM deployments WHERE training_run_id = $1 ORDER BY created_at DESC LIMIT 1"
)


async def get_run_deployment(conn, training_run_id: uuid.UUID) -> RunDeployment | None:
    """The newest deployment of a training run."""
    row = await conn.fetchrow(_RUN_DEPLOYMENT, training_run_id)
    return None if row is None else RunDeployment.model_validate(dict(row))
