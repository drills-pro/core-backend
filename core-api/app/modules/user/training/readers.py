"""What other contexts may ask of training, and the gateway's ownership check (data_plane.access). Each reader takes
the caller's connection, so it is one more query in the caller's transaction, under the same RLS."""
import uuid
from typing import Any

from pydantic import BaseModel, Field

from app.core.enums import Split
from app.integrations.data_plane import Stats
from app.modules.user.training.constants import TrainingStatus
from app.modules.user.training.schemas import LineageRun

# The stats of the dataset version the run trained on (null for a run from before versions), for has_test_split.
_VERSION_STATS = "v.stats AS version_stats"


def has_test_split(test_dataset_id: uuid.UUID | None, version_stats: dict[str, Any] | None) -> bool:
    """Whether the run had data it did not train on that is a test split (planning/06 Quality gates): a test dataset of its own, or
    test rows in its dataset version. A row with no split is hashed into train, validation or test by the trainer, so any unsplit row
    counts too (the stats count only rows that have a split)."""
    if test_dataset_id is not None:
        return True
    if version_stats is None:
        return False
    stats = Stats.model_validate(version_stats)
    return stats.splits.get(Split.TEST, 0) > 0 or stats.row_count > sum(stats.splits.values())


# The report's `classes` are the model's class names (every task); a run reported before that field has only the
# classification confusion-matrix labels, which are the same names.
_RUN_MODEL = (
    "SELECT r.id, r.status, r.mlflow_run_id, COALESCE(r.report -> 'classes', r.report -> 'confusion_matrix' -> 'labels', '[]'::jsonb) AS class_names, r.test_dataset_id, r.metrics, "
    + _VERSION_STATS + " FROM training_runs r LEFT JOIN dataset_versions v ON v.id = r.dataset_version_id WHERE r.id = $1 AND r.project_id = $2"
)
CLASSLESS_POSTPROCESSES = frozenset({"regress"})  # ray_serving/postprocess.py functions that never read class_names
_OWNED = "SELECT 1 FROM training_runs WHERE id = $1 AND project_id = $2 AND tenant_id = $3"


class RunModel(BaseModel):
    """What a training run left behind for the registry and for serving."""

    id: uuid.UUID
    status: TrainingStatus
    mlflow_run_id: str | None
    class_names: list[str] = Field(default_factory=list)
    has_test_split: bool = False  # the deploy gate asks for an acknowledgement without one
    metrics: dict[str, Any] | None = None

    @property
    def has_model(self) -> bool:
        """A succeeded run whose model was put in its MLflow run (the settle step does that before the run succeeds)."""
        return self.status is TrainingStatus.SUCCEEDED and self.mlflow_run_id is not None

    def servable_by(self, postprocess: str | None) -> bool:
        """The classifying postprocesses (classify, detect, ranked_probabilities) name their results from the class names, so a model
        without them cannot be served by them; a regression has no classes and returns a bare number."""
        return self.has_model and (bool(self.class_names) or postprocess in CLASSLESS_POSTPROCESSES)


def _with_split(row) -> dict[str, Any]:
    data = dict(row)
    data["has_test_split"] = has_test_split(data.get("test_dataset_id"), data.pop("version_stats", None))
    return data


async def get_run_model(conn, project_id: uuid.UUID, run_id: uuid.UUID) -> RunModel | None:
    """The run's model facts; None when the run is not one of this project's."""
    row = await conn.fetchrow(_RUN_MODEL, run_id, project_id)
    return None if row is None else RunModel.model_validate(_with_split(row))


_RUN_LINEAGE = (
    "SELECT r.id, r.status, r.dataset_id, r.dataset_version, r.dataset_version_id, r.test_dataset_id, r.test_dataset_version, "
    "r.mlflow_run_id, r.reproduction_group_id, r.config, r.created_at, r.task, r.preset, r.metrics, r.spec->'payload'->'base_model' AS base_model, (r.report ->> 'near_duplicates')::int AS near_duplicates, "
    + _VERSION_STATS + " FROM training_runs r LEFT JOIN dataset_versions v ON v.id = r.dataset_version_id WHERE r.id = $1 AND r.project_id = $2"
)


async def get_run_lineage(conn, project_id: uuid.UUID, run_id: uuid.UUID) -> LineageRun | None:
    """The run's lineage facts; None when the run is not one of this project's."""
    row = await conn.fetchrow(_RUN_LINEAGE, run_id, project_id)
    return None if row is None else LineageRun.model_validate(_with_split(row))


async def owns_training_run(conn, tenant_id: uuid.UUID, run_id: uuid.UUID, project_id: uuid.UUID):
    return await conn.fetchval(_OWNED, run_id, project_id, tenant_id)
