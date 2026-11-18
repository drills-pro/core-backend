"""The unauthenticated /v1/public lists on a real Postgres: core_app (no tenant set) reaches only gallery-shared projects
through the SECURITY DEFINER functions, and the sharing level decides what they show."""
from types import SimpleNamespace

import asyncpg
import pytest

from app.modules.user.projects.constants import Sharing
from app.modules.user.public import service
from tests.support import TenantCtx

SHARE = "UPDATE projects SET sharing = $2, visibility = 'internal' WHERE tenant_id = $1 RETURNING id"
SET_LEVEL = "UPDATE projects SET sharing = $2 WHERE id = $1"


def _as_anonymous(t, monkeypatch):
    async def pool():
        return SimpleNamespace(acquire=lambda: TenantCtx(t.conn))

    monkeypatch.setattr(service, "get_pool", pool)


def test_only_gallery_projects_are_listed_with_their_name(two_tenants, monkeypatch):
    t = two_tenants
    _as_anonymous(t, monkeypatch)

    async def go():
        listed_id = await t.conn.fetchval(SHARE, t.a, Sharing.GALLERY)
        async with t.app_role():
            listed = await service.list_projects(50, 0)
        assert [(p.id, p.name) for p in listed] == [(listed_id, "project")]  # tenant b's private project is not there
        await t.conn.execute(SET_LEVEL, listed_id, Sharing.PRIVATE)
        async with t.app_role():
            assert await service.list_projects(50, 0) == []

    t.run(go())


def test_artifacts_need_the_downloads_level_and_an_unshared_project_lists_nothing(two_tenants, monkeypatch):
    t = two_tenants
    _as_anonymous(t, monkeypatch)

    async def go():
        shared = await t.conn.fetchval(SHARE, t.a, Sharing.GALLERY)
        unshared = await t.conn.fetchval("SELECT id FROM projects WHERE tenant_id = $1", t.b)
        async with t.app_role():
            assert await service.list_datasets(unshared) == [] and await service.list_models(unshared) == []
            models = await service.list_models(shared)
            assert [(m.publish_metadata, m.publish_artifact) for m in models] == [(True, False)]  # metadata only
            assert all(not d.published for d in await service.list_datasets(shared))
        await t.conn.execute(SET_LEVEL, shared, Sharing.GALLERY_DOWNLOADS)
        async with t.app_role():
            assert [m.publish_artifact for m in await service.list_models(shared)] == [True]
        await t.conn.execute("UPDATE model_versions SET publish_artifact = false WHERE project_id = $1", shared)  # "exclude this one"
        async with t.app_role():
            assert [m.publish_artifact for m in await service.list_models(shared)] == [False]

    t.run(go())


def test_a_gallery_level_needs_an_internal_project(two_tenants):
    t = two_tenants

    async def go():
        with pytest.raises(asyncpg.CheckViolationError):
            await t.conn.execute("UPDATE projects SET sharing = $2, visibility = 'private' WHERE tenant_id = $1", t.a, Sharing.GALLERY)

    t.run(go())
