"""FR1 (05-reproducibility-lineage.md): the lineage view composes the readers of training, datasets,
model_registry and deployments. It lives here because deployments already depends on training; neither of them imports the other back."""
import uuid

from app.core.authz import AuthzContext
from app.core.problem import not_found
from app.core.tenant_conn import TenantConn
from app.modules.user.datasets import get_version_ref
from app.modules.user.deployments.readers import get_run_deployment
from app.modules.user.deployments.schemas import LineageModel, TrainingRunLineage
from app.modules.user.model_registry import get_run_version
from app.modules.user.training import get_run_lineage


async def get_lineage(ctx: AuthzContext, project_id: uuid.UUID, run_id: uuid.UUID) -> TrainingRunLineage:
    async with TenantConn(ctx.tenant_id) as conn:
        run = await get_run_lineage(conn, project_id, run_id)
        if run is None:
            raise not_found()
        version = await get_version_ref(conn, run.dataset_version_id) if run.dataset_version_id else None
        model = await get_run_version(conn, run_id)
        deployment = await get_run_deployment(conn, run_id) if model is not None else None
    return TrainingRunLineage(
        training_run=run,
        dataset_version=version,
        model_version=LineageModel.model_validate(model.model_dump()) if model else None,
        deployment=deployment,
    )
