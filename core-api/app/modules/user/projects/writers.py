"""What other contexts may ask of projects to create one, on the caller's connection and transaction."""
import uuid

from app.core.enums import TaskType
from app.modules.user.projects.schemas import CreatedProject

_INSERT = (
    "INSERT INTO projects (tenant_id, name, team_id, task_type) VALUES ($1, $2, $3, $4) "
    "RETURNING id, name, status, team_id, task_type, visibility, default_access, gallery_published, created_at"
)


_DELETE_BINDINGS = "DELETE FROM project_members WHERE tenant_id = $1 AND user_sub = $2"


async def remove_member_bindings(conn, tenant_id: uuid.UUID | str, user_sub: str) -> None:
    """Drop every project binding of a person leaving the tenant (tenant_members calls it through its hook)."""
    await conn.execute(_DELETE_BINDINGS, tenant_id, user_sub)


async def create_project(conn, tenant_id: uuid.UUID | str, name: str, team_id: uuid.UUID, task_type: TaskType) -> CreatedProject:
    return CreatedProject.model_validate(dict(await conn.fetchrow(_INSERT, tenant_id, name, team_id, task_type)))
