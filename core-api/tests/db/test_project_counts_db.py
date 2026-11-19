"""GET /v1/projects counts as core_app on a real Postgres: deleted rows and another tenant's rows are not counted."""
from app.core.authz import Action, AuthzContext, TenantRole
from app.core.enums import DeploymentStatus, TaskType
from app.core.pagination import PageParams
from app.modules.user.projects import service

DATASET = "INSERT INTO datasets (tenant_id, project_id, name, deleted_at) VALUES ($1, $2, $3, $4)"
DELETED_DEPLOYMENT = (
    "INSERT INTO deployments (tenant_id, project_id, training_run_id, route_prefix, status) "
    "SELECT tenant_id, project_id, training_run_id, 'p-' || gen_random_uuid()::text, $2 FROM deployments WHERE tenant_id = $1 LIMIT 1"
)


def test_the_list_counts_only_live_rows_of_the_callers_tenant(two_tenants, monkeypatch):
    t = two_tenants

    async def go():
        project = await t.conn.fetchval(
            "UPDATE projects SET task_type = $2 WHERE tenant_id = $1 RETURNING id", t.a, TaskType.IMAGE_CLASSIFICATION)  # the seed has none
        await t.conn.execute(DATASET, t.a, project, "gone", await t.conn.fetchval("SELECT now()"))
        await t.conn.execute(DATASET, t.b, project, "b's row on a's project", None)  # RLS must hide it from the subquery
        await t.conn.execute(DELETED_DEPLOYMENT, t.a, DeploymentStatus.DELETED)

        async def accessible(self, conn):
            return [project]

        monkeypatch.setattr(AuthzContext, "accessible_project_ids", accessible)
        ctx = AuthzContext(str(t.a), "u1", TenantRole.MEMBER, None, frozenset(Action))
        async with t.app_role():
            page = await service.list_projects(ctx, PageParams(page_size=10, page_token=None))
        [item] = page.items
        assert (item.datasets, item.models, item.deployments) == (1, 1, 1)  # the seed's one of each

    t.run(go())
