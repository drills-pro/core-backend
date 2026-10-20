"""Project lifecycle, settings and member bindings: the business rules and queries behind routes.py."""
import uuid

from fastapi import HTTPException

from app.core import audit
from app.core.authz import AuthzContext, DefaultAccess, Role, TenantRole, Visibility
from app.core.enums import TaskType
from app.core.problem import Problem, not_found
from app.core.tenant_conn import TenantConn
from app.modules.user.projects.constants import (
    NO_TEAM,
    NOTHING_TO_UPDATE,
    STATUS_NOT_SETTABLE,
    AuditAction,
    EntityType,
    ProblemCode,
    ProjectStatus,
)
from app.modules.user.projects.schemas import (
    AuditEntry,
    DeleteResult,
    Member,
    MemberRoles,
    ProjectDetail,
    ProjectSettings,
    ProjectSettingsPatch,
    ProjectSummary,
    ProjectUpdated,
    PublishResult,
    VisibilityResult,
)

_SUMMARY = "id, name, status, team_id, task_type, created_at"


def _live(row):
    """The row of an `... WHERE status != pending_delete` query, or the 404 when it matched nothing."""
    if row is None:
        raise not_found()  # RLS hides other tenants' rows: indistinguishable from a missing one
    return row


async def _record(conn, ctx: AuthzContext, project_id: uuid.UUID, action: AuditAction, entity_type: EntityType, entity_id) -> None:
    await audit.record(conn, tenant_id=ctx.tenant_id, project_id=project_id, actor=ctx.actor,
                       action=action, entity_type=entity_type, entity_id=entity_id)


async def list_projects(ctx: AuthzContext, limit: int, offset: int) -> list[ProjectSummary]:
    async with TenantConn(ctx.tenant_id) as conn:
        ids = await ctx.accessible_project_ids(conn)
        rows = await conn.fetch(
            f"SELECT {_SUMMARY} FROM projects "
            "WHERE id = ANY($1) AND status != $2 ORDER BY created_at LIMIT $3 OFFSET $4",
            ids,
            ProjectStatus.PENDING_DELETE,
            limit,
            offset,
        )
    return [ProjectSummary.model_validate(dict(r)) for r in rows]


async def create_project(ctx: AuthzContext, name: str, task_type: TaskType, team_id: uuid.UUID | None) -> ProjectSummary:
    async with TenantConn(ctx.tenant_id) as conn:
        if team_id is None:
            # Every org has >=1 team (seeded at provisioning); a project with no team_id given lands in
            # the tenant's oldest (Default) team rather than requiring every caller to look it up.
            team_id = await conn.fetchval("SELECT id FROM teams ORDER BY created_at LIMIT 1")
            if team_id is None:
                raise HTTPException(status_code=409, detail=NO_TEAM)
        row = await conn.fetchrow(
            f"INSERT INTO projects (tenant_id, name, team_id, task_type) VALUES ($1, $2, $3, $4) RETURNING {_SUMMARY}",
            ctx.tenant_id,
            name,
            team_id,
            task_type,
        )
        await _record(conn, ctx, row["id"], AuditAction.CREATE, EntityType.PROJECT, row["id"])
        # The creator administers what they create (planning/04: auto-bound project_admin).
        await conn.execute(
            "INSERT INTO project_members (tenant_id, project_id, user_sub, role) VALUES ($1, $2, $3, $4)",
            ctx.tenant_id,
            row["id"],
            ctx.actor,
            Role.PROJECT_ADMIN,
        )
    return ProjectSummary.model_validate(dict(row))


async def get_project(ctx: AuthzContext, project_id: uuid.UUID) -> ProjectDetail:
    async with TenantConn(ctx.tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT id, name, status, team_id, task_type, default_test_dataset_id, settings, "
            "visibility, default_access, publish_project_metadata, created_at "
            "FROM projects WHERE id = $1",
            project_id,
        )
    return ProjectDetail.model_validate(dict(_live(row)))


async def update_project(
    ctx: AuthzContext,
    project_id: uuid.UUID,
    name: str | None,
    status: ProjectStatus | None,
    default_test_dataset_id: uuid.UUID | None,
) -> ProjectUpdated:
    # `status` covers archive/reactivate; delete is its own operation (grace period).
    if status is ProjectStatus.PENDING_DELETE:
        raise HTTPException(status_code=400, detail=STATUS_NOT_SETTABLE)
    if name is None and status is None and default_test_dataset_id is None:
        raise HTTPException(status_code=400, detail=NOTHING_TO_UPDATE)

    async with TenantConn(ctx.tenant_id) as conn:
        if default_test_dataset_id is not None:
            owned = await conn.fetchval(
                "SELECT 1 FROM datasets WHERE id = $1 AND project_id = $2", default_test_dataset_id, project_id
            )
            if not owned:
                raise not_found()
        row = _live(
            await conn.fetchrow(
                "UPDATE projects SET name = COALESCE($2, name), status = COALESCE($3, status), "
                "default_test_dataset_id = COALESCE($4, default_test_dataset_id) "
                "WHERE id = $1 AND status != $5 "
                f"RETURNING {_SUMMARY}, default_test_dataset_id",
                project_id,
                name,
                status,
                default_test_dataset_id,
                ProjectStatus.PENDING_DELETE,
            )
        )
        await _record(conn, ctx, project_id, AuditAction.UPDATE, EntityType.PROJECT, project_id)
    return ProjectUpdated.model_validate(dict(row))


async def update_settings(ctx: AuthzContext, project_id: uuid.UUID, patch: ProjectSettingsPatch) -> ProjectSettings:
    # Merged into the existing jsonb blob, not replaced.
    values = patch.model_dump(mode="json", exclude_none=True)
    if not values:
        raise HTTPException(status_code=400, detail=NOTHING_TO_UPDATE)
    async with TenantConn(ctx.tenant_id) as conn:
        row = _live(
            await conn.fetchrow(
                "UPDATE projects SET settings = settings || $2::jsonb "
                "WHERE id = $1 AND status != $3 RETURNING id, settings",
                project_id,
                values,
                ProjectStatus.PENDING_DELETE,
            )
        )
    return ProjectSettings.model_validate(dict(row))


async def update_visibility(
    ctx: AuthzContext, project_id: uuid.UUID, visibility: Visibility | None, default_access: DefaultAccess | None
) -> VisibilityResult:
    # Who can see the project (visibility) and what `internal` grants every tenant member
    # (default_access, planning/04) are both project_admin decisions.
    if visibility is None and default_access is None:
        raise HTTPException(status_code=400, detail=NOTHING_TO_UPDATE)
    async with TenantConn(ctx.tenant_id) as conn:
        # A gallery project must stay internal (CHECK): refuse rather than silently unpublish it.
        if visibility is Visibility.PRIVATE and await conn.fetchval(
            "SELECT gallery_published FROM projects WHERE id = $1", project_id
        ):
            raise Problem(409, ProblemCode.GALLERY_PUBLISHED, "unpublish the project from the gallery before making it private")
        row = _live(
            await conn.fetchrow(
                "UPDATE projects SET visibility = COALESCE($2, visibility), "
                "default_access = COALESCE($3, default_access) "
                "WHERE id = $1 AND status != $4 "
                "RETURNING id, visibility, default_access",
                project_id,
                visibility,
                default_access,
                ProjectStatus.PENDING_DELETE,
            )
        )
    return VisibilityResult.model_validate(dict(row))


async def update_publish(ctx: AuthzContext, project_id: uuid.UUID, publish_project_metadata: bool) -> PublishResult:
    # The metadata-publish flag is independent of visibility.
    async with TenantConn(ctx.tenant_id) as conn:
        row = _live(
            await conn.fetchrow(
                "UPDATE projects SET publish_project_metadata = $2 WHERE id = $1 AND status != $3 "
                "RETURNING id, publish_project_metadata",
                project_id,
                publish_project_metadata,
                ProjectStatus.PENDING_DELETE,
            )
        )
    return PublishResult.model_validate(dict(row))


async def delete_project(ctx: AuthzContext, project_id: uuid.UUID) -> DeleteResult:
    # Soft delete (30-day grace): a background job (not built yet) hard-deletes rows where
    # deleted_at < now() - interval '30 days'.
    async with TenantConn(ctx.tenant_id) as conn:
        row = _live(
            await conn.fetchrow(
                "UPDATE projects SET status = $2, deleted_at = now() WHERE id = $1 AND status != $2 RETURNING id",
                project_id,
                ProjectStatus.PENDING_DELETE,
            )
        )
        await _record(conn, ctx, project_id, AuditAction.DELETE, EntityType.PROJECT, project_id)
    return DeleteResult(id=row["id"], status=ProjectStatus.PENDING_DELETE)


async def _keep_an_admin(conn, ctx: AuthzContext, project_id: uuid.UUID, user_sub: str) -> None:
    """Refuse to strip the last project_admin; tenant owner/admin can always recover the project, so they may."""
    if ctx.tenant_role in (TenantRole.OWNER, TenantRole.ADMIN):
        return
    # ponytail: unlocked check, a concurrent pair of removals could still leave none; owner/admin recover it.
    if await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM project_members WHERE project_id = $1 AND user_sub = $2 AND role = $3) "
        "AND NOT EXISTS (SELECT 1 FROM project_members WHERE project_id = $1 AND user_sub <> $2 AND role = $3)",
        project_id,
        user_sub,
        Role.PROJECT_ADMIN,
    ):
        raise Problem(409, ProblemCode.LAST_PROJECT_ADMIN, "a project needs at least one project_admin")


async def list_members(ctx: AuthzContext, project_id: uuid.UUID) -> list[Member]:
    """One entry per person with their bound roles (`internal` access is not listed: it is not a binding)."""
    async with TenantConn(ctx.tenant_id) as conn:
        rows = await conn.fetch(
            "SELECT user_sub, array_agg(role ORDER BY role) AS roles, min(created_at) AS created_at "
            "FROM project_members WHERE project_id = $1 GROUP BY user_sub ORDER BY min(created_at)",
            project_id,
        )
    return [Member.model_validate(dict(r)) for r in rows]


async def set_member_roles(ctx: AuthzContext, project_id: uuid.UUID, user_sub: str, new_roles: list[Role]) -> MemberRoles:
    """Replace the person's bindings with exactly these roles. Targets must be tenant members."""
    roles = sorted({r.value for r in new_roles})
    async with TenantConn(ctx.tenant_id) as conn:
        if not await conn.fetchval("SELECT 1 FROM tenant_members WHERE user_sub = $1", user_sub):
            raise Problem(400, ProblemCode.NOT_A_TENANT_MEMBER, "only tenant members can be bound to a project")
        if Role.PROJECT_ADMIN not in roles:
            await _keep_an_admin(conn, ctx, project_id, user_sub)
        await conn.execute(
            "DELETE FROM project_members WHERE project_id = $1 AND user_sub = $2 AND role <> ALL($3::text[])",
            project_id,
            user_sub,
            roles,
        )
        await conn.execute(
            "INSERT INTO project_members (tenant_id, project_id, user_sub, role) "
            "SELECT $1, $2, $3, r FROM unnest($4::text[]) r ON CONFLICT DO NOTHING",
            ctx.tenant_id,
            project_id,
            user_sub,
            roles,
        )
        await _record(conn, ctx, project_id, AuditAction.SET_MEMBER_ROLES, EntityType.PROJECT_MEMBER, user_sub)
    return MemberRoles(project_id=project_id, user_sub=user_sub, roles=roles)


async def remove_member(ctx: AuthzContext, project_id: uuid.UUID, user_sub: str) -> None:
    """Remove all of the person's bindings."""
    async with TenantConn(ctx.tenant_id) as conn:
        await _keep_an_admin(conn, ctx, project_id, user_sub)
        result = await conn.execute(
            "DELETE FROM project_members WHERE project_id = $1 AND user_sub = $2", project_id, user_sub
        )
        if result == "DELETE 0":
            raise not_found()
        await _record(conn, ctx, project_id, AuditAction.REMOVE_MEMBER, EntityType.PROJECT_MEMBER, user_sub)


async def audit_log(ctx: AuthzContext, project_id: uuid.UUID, limit: int, offset: int) -> list[AuditEntry]:
    async with TenantConn(ctx.tenant_id) as conn:
        rows = await conn.fetch(
            "SELECT id, actor, action, entity_type, entity_id, created_at FROM audit_log "
            "WHERE project_id = $1 ORDER BY created_at DESC LIMIT $2 OFFSET $3",
            project_id,
            limit,
            offset,
        )
    return [AuditEntry.model_validate(dict(r)) for r in rows]
