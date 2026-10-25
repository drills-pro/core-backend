"""What follows an import's row reaching an end state (cancelled, failed, expired, discarded, committed): stop its
job, then purge its staging. Always after the status change has committed, and never raising: a failure only
leaves files behind (the dataset's final purge removes them) or a job that times out by itself, whereas doing
either first could leave a reviewable import with no data."""
import logging

from app.integrations import compute, data_plane

logger = logging.getLogger(__name__)


async def stop_job_quietly(job_id: str) -> None:
    try:
        await compute.stop(job_id)
    except Exception:  # noqa: BLE001 - the job may already be gone
        logger.warning("could not stop ingest job %s", job_id)


async def purge_staging_quietly(tenant_id: str, dataset_id: str, import_id: str) -> None:
    try:
        await data_plane.purge_staging(tenant_id, dataset_id, import_id)
    except Exception:  # noqa: BLE001 - see the module docstring
        logger.exception("could not purge staging of import %s", import_id)


async def wind_down(row: dict, *, stop_job: bool = True) -> None:
    """Stop the import's job (unless it already ended: `stop_job=False`), then purge its staging."""
    if stop_job and row["ray_job_id"]:
        await stop_job_quietly(row["ray_job_id"])
    await purge_staging_quietly(str(row["tenant_id"]), str(row["dataset_id"]), str(row["id"]))
