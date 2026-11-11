"""What other contexts may ask of training, and the gateway's ownership check (data_plane.access). Each reader takes
the caller's connection, so it is one more query in the caller's transaction, under the same RLS."""
import uuid

from pydantic import BaseModel, Field

from app.modules.user.training.constants import TrainingStatus
from app.modules.user.training.schemas import LineageRun

# The report's `classes` are the model's class names (every task); a run reported before that field has only the
# classification confusion-matrix labels, which are the same names.
_RUN_MODEL = (
    "SELECT id, status, mlflow_run_id, COALESCE(report -> 'classes', report -> 'confusion_matrix' -> 'labels', '[]'::jsonb) AS class_names, test_dataset_id "
    "FROM training_runs WHERE id = $1 AND project_id = $2"
)
_OWNED = "SELECT 1 FROM training_runs WHERE id = $1 AND project_id = $2 AND tenant_id = $3"


class RunModel(BaseModel):
    """What a training run left behind for the registry and for serving."""

    id: uuid.UUID
    status: TrainingStatus
    mlflow_run_id: str | None
    class_names: list[str] = Field(default_factory=list)
    test_dataset_id: uuid.UUID | None = None

    @property
    def has_test_split(self) -> bool:
        """The run was scored on a dataset of its own, not only on what it trained on (the deploy gate asks for an acknowledgement without one)."""
        return self.test_dataset_id is not None

    @property
    def has_model(self) -> bool:
        """A succeeded run whose model was put in its MLflow run (the settle step does that before the run succeeds)."""
        return self.status is TrainingStatus.SUCCEEDED and self.mlflow_run_id is not None

    @property
    def servable(self) -> bool:
        """Both served postprocesses (classify, detect) name their results from the class names, so a model without them cannot be served."""
        return self.has_model and bool(self.class_names)


async def get_run_model(conn, project_id: uuid.UUID, run_id: uuid.UUID) -> RunModel | None:
    """The run's model facts; None when the run is not one of this project's."""
    row = await conn.fetchrow(_RUN_MODEL, run_id, project_id)
    return None if row is None else RunModel.model_validate(dict(row))


_RUN_LINEAGE = (
    "SELECT id, status, dataset_id, dataset_version, dataset_version_id, test_dataset_id, test_dataset_version, "
    "mlflow_run_id, reproduction_group_id, config, created_at, task, preset, metrics FROM training_runs WHERE id = $1 AND project_id = $2"
)


async def get_run_lineage(conn, project_id: uuid.UUID, run_id: uuid.UUID) -> LineageRun | None:
    """The run's lineage facts; None when the run is not one of this project's."""
    row = await conn.fetchrow(_RUN_LINEAGE, run_id, project_id)
    return None if row is None else LineageRun.model_validate(dict(row))


async def owns_training_run(conn, tenant_id: uuid.UUID, run_id: uuid.UUID, project_id: uuid.UUID):
    return await conn.fetchval(_OWNED, run_id, project_id, tenant_id)
