"""Project lifecycle, settings and member bindings: the business rules and queries behind routes.py."""
import uuid
from http import HTTPStatus

from app.core import audit, task_types
from app.core.authz import AuthzContext, DefaultAccess, Role, TenantRole, Visibility
from app.core.enums import DeploymentStatus, TaskType
from app.core.metrics import usage as usage_metrics
from app.core.pagination import Page, PageParams, SortDir, build_page, keyset_clause, order_by
from app.core.problem import Problem, not_found
from app.core.tenant_conn import TenantConn
from app.modules.user.teams import DEFAULT_TEAM_NAME, create_team, get_default_team_id
from app.modules.user.tenant_members import is_member
from app.modules.user.projects import readers
from app.modules.user.projects.writers import create_project as insert_project
from app.modules.user.projects.constants import (
    NOTHING_TO_UPDATE,
    STATUS_NOT_SETTABLE,
    AuditAction,
    EntityType,
    ProblemCode,
    ProjectStatus,
    Sharing,
)
from app.modules.user.projects.schemas import (
    AuditEntry,
    DeleteResult,
    Member,
    MemberRoles,
    ProjectDetail,
    ProjectListItem,
    ProjectSettings,
    ProjectSettingsPatch,
    ProjectSummary,
    ProjectUpdated,
    SharingResult,
    TrashedProject,
    VisibilityResult,
)

_SUMMARY = "id, name, status, team_id, task_type, created_at"
# The project card's counts; $3 is DeploymentStatus.DELETED. Each table indexes project_id, and RLS scopes the subqueries.
_COUNTS = (
    "(SELECT count(*) FROM datasets d WHERE d.project_id = projects.id AND d.deleted_at IS NULL) AS datasets, "
    "(SELECT count(*) FROM models m WHERE m.project_id = projects.id) AS models, "
    "(SELECT count(*) FROM deployments x WHERE x.project_id = projects.id AND x.status != $3) AS deployments"
)


def _live(row):
    """The row of an `... WHERE status != pending_delete` query, or the 404 when it matched nothing."""
    if row is None:
        raise not_found()  # RLS hides other tenants' rows: indistinguishable from a missing one
    return row


async def list_projects(ctx: AuthzContext, page: PageParams) -> Page[ProjectListItem]:
    async with TenantConn(ctx.tenant_id) as conn:
        ids = await ctx.accessible_project_ids(conn)
        cond, args = keyset_clause(page.page_token, SortDir.ASC, first_param=4)
        rows = await conn.fetch(
            f"SELECT {_SUMMARY}, {_COUNTS} FROM projects WHERE id = ANY($1) AND status != $2 {cond} "
            f"ORDER BY {order_by(SortDir.ASC)} LIMIT {page.page_size + 1}",
            ids,
            ProjectStatus.PENDING_DELETE,
            DeploymentStatus.DELETED,
            *args,
        )
    return build_page(rows, page.page_size, ProjectListItem)


async def list_trashed_projects(ctx: AuthzContext, page: PageParams) -> Page[TrashedProject]:
    async with TenantConn(ctx.tenant_id) as conn:
        ids = await ctx.accessible_project_ids(conn)
        cond, args = keyset_clause(page.page_token, SortDir.ASC, first_param=3)
        rows = await conn.fetch(
            "SELECT id, name, created_at, deleted_at FROM projects WHERE id = ANY($1) AND status = $2 "
            f"{cond} ORDER BY {order_by(SortDir.ASC)} LIMIT {page.page_size + 1}",
            ids,
            ProjectStatus.PENDING_DELETE,
            *args,
        )
    return build_page(rows, page.page_size, TrashedProject)


async def create_project(ctx: AuthzContext, name: str, task_type: TaskType, team_id: uuid.UUID | None) -> ProjectSummary:
    async with TenantConn(ctx.tenant_id) as conn:
        await task_types.require_supported(conn, task_type)
        if team_id is None:
            # A project with no team_id given lands in the tenant's oldest (Default) team rather than
            # requiring every caller to look it up. Orgs are seeded with one, but it can be deleted:
            # recreate it here, in this transaction, so the caller never sees a "no team" error.
            team_id = await get_default_team_id(conn)
            if team_id is None:
                team_id = (await create_team(conn, ctx.tenant_id, DEFAULT_TEAM_NAME)).id
        created = await insert_project(conn, ctx.tenant_id, name, team_id, task_type, ctx.actor)
        # The creator administers what they create (planning/04: auto-bound project_admin).
        await conn.execute(
            "INSERT INTO project_members (tenant_id, project_id, user_sub, role) VALUES ($1, $2, $3, $4)",
            ctx.tenant_id,
            created.id,
            ctx.actor,
            Role.PROJECT_ADMIN,
        )
    usage_metrics.projects_created.add(1, {"task_type": task_type})
    return ProjectSummary.model_validate(created.model_dump())


async def get_project(ctx: AuthzContext, project_id: uuid.UUID) -> ProjectDetail:
    async with TenantConn(ctx.tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT id, name, status, team_id, task_type, default_test_dataset_id, settings, "
            "visibility, default_access, sharing, created_at "
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
        raise Problem(HTTPStatus.BAD_REQUEST, ProblemCode.STATUS_NOT_SETTABLE, STATUS_NOT_SETTABLE)
    if name is None and status is None and default_test_dataset_id is None:
        raise Problem(HTTPStatus.BAD_REQUEST, ProblemCode.NOTHING_TO_UPDATE, NOTHING_TO_UPDATE)

    async with TenantConn(ctx.tenant_id) as conn:
        if default_test_dataset_id is not None:
            if not await readers.has_dataset(conn, project_id, default_test_dataset_id):
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
        await audit.record_for(conn, ctx, project_id, AuditAction.UPDATE, EntityType.PROJECT, project_id)
    return ProjectUpdated.model_validate(dict(row))


async def update_settings(ctx: AuthzContext, project_id: uuid.UUID, patch: ProjectSettingsPatch) -> ProjectSettings:
    # Merged into the existing jsonb blob, not replaced.
    values = patch.model_dump(mode="json", exclude_none=True)
    if not values:
        raise Problem(HTTPStatus.BAD_REQUEST, ProblemCode.NOTHING_TO_UPDATE, NOTHING_TO_UPDATE)
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
    # (default_access, planning/04) are both project_admin decisions. Private also takes it out of the gallery.
    if visibility is None and default_access is None:
        raise Problem(HTTPStatus.BAD_REQUEST, ProblemCode.NOTHING_TO_UPDATE, NOTHING_TO_UPDATE)
    async with TenantConn(ctx.tenant_id) as conn:
        row = _live(
            await conn.fetchrow(
                "UPDATE projects SET visibility = COALESCE($2, visibility), "
                "default_access = COALESCE($3, default_access), "
                "sharing = CASE WHEN $2 = $5 THEN $6 ELSE sharing END "
                "WHERE id = $1 AND status != $4 "
                "RETURNING id, visibility, default_access, sharing",
                project_id,
                visibility,
                default_access,
                ProjectStatus.PENDING_DELETE,
                Visibility.PRIVATE,
                Sharing.PRIVATE,
            )
        )
    return VisibilityResult.model_validate(dict(row))


async def update_sharing(ctx: AuthzContext, project_id: uuid.UUID, sharing: Sharing) -> SharingResult:
    # One choice replaces the old gallery flags. A gallery level needs an internal project (CHECK), so it sets
    # that too; Private leaves visibility alone and just unpublishes, with no 409.
    async with TenantConn(ctx.tenant_id) as conn:
        row = _live(
            await conn.fetchrow(
                "UPDATE projects SET sharing = $2, "
                "visibility = CASE WHEN $2 = $4 THEN visibility ELSE $5 END "
                "WHERE id = $1 AND status != $3 RETURNING id, sharing, visibility",
                project_id,
                sharing,
                ProjectStatus.PENDING_DELETE,
                Sharing.PRIVATE,
                Visibility.INTERNAL,
            )
        )
        await audit.record_for(conn, ctx, project_id, AuditAction.SET_SHARING, EntityType.PROJECT, project_id)
    return SharingResult.model_validate(dict(row))


async def delete_project(ctx: AuthzContext, project_id: uuid.UUID) -> DeleteResult:
    # Soft delete (30-day grace): a background job (not built yet) hard-deletes rows where
    # deleted_at < now() - interval '30 days'.
    async with TenantConn(ctx.tenant_id) as conn:
        row = _live(
            await conn.fetchrow(
                "UPDATE projects SET status = $2, deleted_at = now() WHERE id = $1 AND status != $2 RETURNING id, deleted_at",
                project_id,
                ProjectStatus.PENDING_DELETE,
            )
        )
        await audit.record_for(conn, ctx, project_id, AuditAction.DELETE, EntityType.PROJECT, project_id)
    return DeleteResult(id=row["id"], status=ProjectStatus.PENDING_DELETE, deleted_at=row["deleted_at"])


async def restore_project(ctx: AuthzContext, project_id: uuid.UUID) -> ProjectSummary:
    """Undo a soft delete inside the grace period. The pre-delete status is not kept, so it comes back `active`."""
    async with TenantConn(ctx.tenant_id) as conn:
        row = await conn.fetchrow(
            "UPDATE projects SET status = $2, deleted_at = NULL WHERE id = $1 AND status = $3 "
            f"RETURNING {_SUMMARY}",
            project_id,
            ProjectStatus.ACTIVE,
            ProjectStatus.PENDING_DELETE,
        )
        if row is None:  # authz already proved the project exists, so it is simply not deleted
            raise Problem(409, ProblemCode.NOT_PENDING_DELETE, "this project is not pending deletion")
        await audit.record_for(conn, ctx, project_id, AuditAction.RESTORE, EntityType.PROJECT, project_id)
    return ProjectSummary.model_validate(dict(row))


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
        if not await is_member(conn, user_sub):
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
        await audit.record_for(conn, ctx, project_id, AuditAction.SET_MEMBER_ROLES, EntityType.PROJECT_MEMBER, user_sub)
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
        await audit.record_for(conn, ctx, project_id, AuditAction.REMOVE_MEMBER, EntityType.PROJECT_MEMBER, user_sub)


async def audit_log(ctx: AuthzContext, project_id: uuid.UUID, page: PageParams) -> Page[AuditEntry]:
    async with TenantConn(ctx.tenant_id) as conn:
        cond, args = keyset_clause(page.page_token, SortDir.DESC, first_param=2)
        rows = await conn.fetch(
            "SELECT id, actor, action, entity_type, entity_id, created_at FROM audit_log "
            f"WHERE project_id = $1 {cond} ORDER BY {order_by(SortDir.DESC)} LIMIT {page.page_size + 1}",
            project_id,
            *args,
        )
    return build_page(rows, page.page_size, AuditEntry)
