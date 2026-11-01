"""What other contexts may ask of projects to create one, on the caller's connection and transaction."""
import uuid

from app.core.enums import TaskType
from app.modules.user.projects.schemas import CreatedProject

_INSERT = (
    "INSERT INTO projects (tenant_id, name, team_id, task_type) VALUES ($1, $2, $3, $4) "
    "RETURNING id, name, status, team_id, task_type, visibility, default_access, gallery_published, created_at"
)


async def create_project(conn, tenant_id: uuid.UUID | str, name: str, team_id: uuid.UUID, task_type: TaskType) -> CreatedProject:
    return CreatedProject.model_validate(dict(await conn.fetchrow(_INSERT, tenant_id, name, team_id, task_type)))
