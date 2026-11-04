import uuid

from app.core.db import get_pool
from app.modules.user.public.schemas import PublicDataset, PublicModel, PublicProject

_PROJECTS = "SELECT * FROM list_public_projects($1, $2)"
_DATASETS = "SELECT * FROM list_public_project_datasets($1)"
_MODELS = "SELECT * FROM list_public_project_models($1)"


async def list_projects(limit: int, offset: int) -> list[PublicProject]:
    async with (await get_pool()).acquire() as conn:
        rows = await conn.fetch(_PROJECTS, limit, offset)
    return [PublicProject.model_validate(dict(r)) for r in rows]


async def list_datasets(project_id: uuid.UUID) -> list[PublicDataset]:
    async with (await get_pool()).acquire() as conn:
        rows = await conn.fetch(_DATASETS, project_id)
    return [PublicDataset.model_validate(dict(r)) for r in rows]


async def list_models(project_id: uuid.UUID) -> list[PublicModel]:
    async with (await get_pool()).acquire() as conn:
        rows = await conn.fetch(_MODELS, project_id)
    return [PublicModel.model_validate(dict(r)) for r in rows]
