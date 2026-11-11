"""The unauthenticated /v1/public lists on a real Postgres: core_app (no tenant set) reaches only gallery-published projects
through the SECURITY DEFINER functions, and sees no name unless the owner published it."""
from types import SimpleNamespace

from app.modules.user.public import service
from tests.support import TenantCtx

PUBLISH = "UPDATE projects SET gallery_published = true, publish_project_metadata = $2 WHERE tenant_id = $1 RETURNING id"


def _as_anonymous(t, monkeypatch):
    async def pool():
        return SimpleNamespace(acquire=lambda: TenantCtx(t.conn))

    monkeypatch.setattr(service, "get_pool", pool)


def test_only_gallery_projects_are_listed_and_the_name_needs_the_owners_opt_in(two_tenants, monkeypatch):
    t = two_tenants
    _as_anonymous(t, monkeypatch)

    async def go():
        listed_id = await t.conn.fetchval(PUBLISH, t.a, False)
        async with t.app_role():
            listed = await service.list_projects(50, 0)
        assert [p.id for p in listed] == [listed_id]  # tenant b's unpublished project is not there
        assert (listed[0].name, listed[0].task_type) == (None, None)  # "exists" is visible, contents are not
        await t.conn.execute("UPDATE projects SET publish_project_metadata = true WHERE id = $1", listed_id)
        async with t.app_role():
            assert [p.name for p in await service.list_projects(50, 0)] == ["project"]

    t.run(go())


def test_a_private_dataset_of_a_gallery_project_has_no_name_and_an_unpublished_project_lists_nothing(two_tenants, monkeypatch):
    t = two_tenants
    _as_anonymous(t, monkeypatch)

    async def go():
        published = await t.conn.fetchval(PUBLISH, t.a, True)
        unpublished = await t.conn.fetchval("SELECT id FROM projects WHERE tenant_id = $1", t.b)
        async with t.app_role():
            datasets = await service.list_datasets(published)
            assert [(d.name, d.published) for d in datasets] == [(None, False)]
            assert await service.list_datasets(unpublished) == [] and await service.list_models(unpublished) == []

    t.run(go())
