"""What a settling training run does outside Postgres: the object store, MLflow, Ray. Takes ids, never a connection."""
import logging
from datetime import datetime

import httpx
from pydantic import ValidationError

from app.integrations import compute, data_plane
from app.integrations.data_plane import mlflow
from app.integrations.data_plane.train_report import FailureCode, TrainReport
from app.modules.user.model_registry import RegisteredVersion, register_in_mlflow
from app.modules.user.training import decide
from app.modules.user.training.constants import DEFAULT_MODEL_NAME, TrainingStatus

logger = logging.getLogger(__name__)


async def final_report(tenant_id: str, row: dict) -> TrainReport | None:
    """The job's report; None when it wrote none, a failed report when it wrote one core-api cannot read. A store
    that does not answer raises, and the next sweep tries again."""
    try:
        return await data_plane.read_train_report(tenant_id, str(row["project_id"]), str(row["id"]))
    except ValidationError:
        logger.warning("run %s wrote a report core-api cannot read", row["id"], exc_info=True)
        return decide.failed_report(FailureCode.INTERNAL, "the training job wrote a report that could not be read")


async def ray_started_at(row: dict) -> datetime | None:
    """When Ray started a job that ended on its own before the sweep saw it running (so the run has no started_at).
    Ray's own record, never the job's report (untrusted). None when Ray cannot say: the run is then not billed."""
    try:
        return (await compute.job_state(row["ray_job_id"])).started_at
    except Exception:  # noqa: BLE001 - an unanswered question must not block the settle; the run just bills nothing
        logger.warning("no start time from ray for run %s", row["id"], exc_info=True)
        return None


async def close_mlflow(tenant_id: str, row: dict, outcome: decide.Outcome) -> None:
    """Close the run's MLflow run. A cancel is best effort (a tracking hiccup must not block it); an end on the job's
    own account raises, so the sweep retries."""
    run_id = row["mlflow_run_id"]
    if run_id is None:
        return
    project_id = str(row["project_id"])
    if outcome.status is TrainingStatus.CANCELLED:
        try:
            await mlflow.finish_run(tenant_id, project_id, run_id, mlflow.RunStatus.KILLED)
        except httpx.HTTPError:
            logger.warning("could not close MLflow run %s as killed", run_id, exc_info=True)
    elif outcome.report is not None:
        status = mlflow.RunStatus.FINISHED if outcome.status is TrainingStatus.SUCCEEDED else mlflow.RunStatus.FAILED
        # scalars only: MLflow's log-metric API takes nothing else; history and the confusion matrix stay in the report
        await mlflow.finish_run(tenant_id, project_id, run_id, status, outcome.report.metrics)


async def publish_model(tenant_id: str, row: dict) -> None:
    """Put the trained model in MLflow; repeating it for a retry is harmless."""
    if row["mlflow_run_id"] is not None:
        await mlflow.publish_run_model(tenant_id, str(row["project_id"]), str(row["id"]), row["mlflow_run_id"])


async def register_model(tenant_id: str, row: dict) -> RegisteredVersion | None:
    """Create the run's MLflow model version, with no connection held. Idempotent per MLflow run (the version is
    looked up before it is created), so a retry after a rolled-back settle reuses it; the model_versions row is
    written only by the compare-and-set winner."""
    if row["mlflow_run_id"] is None:
        return None
    return await register_in_mlflow(tenant_id, row["project_id"], DEFAULT_MODEL_NAME, row["mlflow_run_id"])
