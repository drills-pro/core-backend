"""What other contexts may ask of projects to create one, on the caller's connection and transaction."""
import uuid

from app.core import audit
from app.core.enums import TaskType
from app.modules.user.projects.constants import AuditAction, EntityType
from app.modules.user.projects.schemas import CreatedProject

_INSERT = (
    "INSERT INTO projects (tenant_id, name, team_id, task_type) VALUES ($1, $2, $3, $4) "
    "RETURNING id, name, status, team_id, task_type, visibility, default_access, created_at"
)


_DELETE_BINDINGS = "DELETE FROM project_members WHERE tenant_id = $1 AND user_sub = $2"


async def remove_member_bindings(conn, tenant_id: uuid.UUID | str, user_sub: str) -> None:
    """Drop every project binding of a person leaving the tenant (tenant_members calls it through its hook)."""
    await conn.execute(_DELETE_BINDINGS, tenant_id, user_sub)


async def create_project(
    conn, tenant_id: uuid.UUID | str, name: str, team_id: uuid.UUID, task_type: TaskType, actor: str
) -> CreatedProject:
    """Insert the project and its audit row, as the user-facing create does."""
    created = CreatedProject.model_validate(dict(await conn.fetchrow(_INSERT, tenant_id, name, team_id, task_type)))
    await audit.record(conn, tenant_id=str(tenant_id), project_id=created.id, actor=actor,
                       action=AuditAction.CREATE, entity_type=EntityType.PROJECT, entity_id=created.id)
    return created
