"""What other contexts may ask of datasets, and the gateway's ownership checks (data_plane.access). Each reader takes
the caller's connection, so it is one more query in the caller's transaction, under the same RLS."""
import uuid

from pydantic import BaseModel

from app.core.enums import TaskType
from app.core.problem import not_found
from app.modules.user.projects import get_task_type

_LIVE_DATASET = "SELECT 1 FROM datasets WHERE project_id = $1 AND id = $2 AND deleted_at IS NULL"
_IN_PROJECT = "SELECT 1 FROM datasets WHERE id = $1 AND project_id = $2"
_VERSION_SOURCE = (
    "SELECT v.dataset_id, v.lakefs_commit FROM dataset_versions v JOIN datasets d ON d.id = v.dataset_id "
    "WHERE v.id = $1 AND d.project_id = $2 AND d.deleted_at IS NULL"
)
_DATASET = "SELECT 1 FROM datasets WHERE id = $1 AND tenant_id = $2"
_IMPORT = "SELECT 1 FROM dataset_imports WHERE id = $1 AND dataset_id = $2 AND tenant_id = $3"
_VERSION = "SELECT lakefs_commit FROM dataset_versions WHERE id = $1 AND dataset_id = $2 AND tenant_id = $3"


class VersionSource(BaseModel):
    """Where a dataset version's data lives: what a training run needs to read it."""

    dataset_id: uuid.UUID
    lakefs_commit: str


async def get_version_source(conn, project_id: uuid.UUID, version_id: uuid.UUID) -> VersionSource | None:
    """A version of a live dataset of this project; None for any other (another project's or tenant's is the same as a missing one)."""
    row = await conn.fetchrow(_VERSION_SOURCE, version_id, project_id)
    return None if row is None else VersionSource.model_validate(dict(row))


async def dataset_task(conn, project_id: uuid.UUID, dataset_id: uuid.UUID) -> TaskType | None:
    """The task type of a live dataset's project (None when the project has none); 404 when the dataset is not a live one of the project."""
    if await conn.fetchval(_LIVE_DATASET, project_id, dataset_id) is None:  # RLS hides other tenants' rows
        raise not_found()
    return await get_task_type(conn, project_id)


async def in_project(conn, project_id: uuid.UUID, dataset_id: uuid.UUID) -> bool:
    """Whether the dataset (live or not) belongs to the project."""
    return await conn.fetchval(_IN_PROJECT, dataset_id, project_id) is not None


async def owns_dataset(conn, tenant_id: uuid.UUID, dataset_id: uuid.UUID):
    return await conn.fetchval(_DATASET, dataset_id, tenant_id)


async def owns_import(conn, tenant_id: uuid.UUID, import_id: uuid.UUID, dataset_id: uuid.UUID):
    return await conn.fetchval(_IMPORT, import_id, dataset_id, tenant_id)


async def owns_version(conn, tenant_id: uuid.UUID, version_id: uuid.UUID, dataset_id: uuid.UUID):
    """The version's lakeFS commit."""
    return await conn.fetchval(_VERSION, version_id, dataset_id, tenant_id)
