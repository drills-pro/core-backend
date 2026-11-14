"""model_catalog on a real Postgres as core_app: platform data every tenant reads, the CHECKs, and delete-if-unused."""
import asyncpg
import pytest

from app.core.enums import CatalogStatus
from app.modules.admin.model_catalog import service

INSERT = (
    "INSERT INTO model_catalog (repo_id, revision, display_name, task, license, status, created_by, updated_by) "
    "VALUES ('org/new', $1, 'new', 'image-classification', 'mit', $2, 'ops', 'ops') RETURNING id"
)
SHA = "b" * 40


def test_the_presets_base_models_were_seeded_and_every_tenant_reads_them(two_tenants):
    async def read():
        async with two_tenants.as_tenant(two_tenants.a) as conn:
            return await conn.fetchval("SELECT count(*) FROM model_catalog WHERE status = $1", CatalogStatus.ENABLED)

    assert two_tenants.run(read()) > 0


def test_core_app_can_create_update_and_delete_an_unpinned_entry(two_tenants):
    async def lifecycle():
        async with two_tenants.app_role() as conn:
            model_id = await conn.fetchval(INSERT, SHA, CatalogStatus.ENABLED)
            await conn.execute("UPDATE model_catalog SET status = $2 WHERE id = $1", model_id, CatalogStatus.DISABLED)
            assert await conn.fetchval(service._DELETE_UNUSED, model_id) == model_id
            assert await conn.fetchval(service._EXISTS, model_id) is False

    two_tenants.run(lifecycle())


def test_an_entry_a_preset_pins_is_not_deleted(two_tenants):
    async def pinned():
        async with two_tenants.app_role() as conn:
            model_id = await conn.fetchval("SELECT c.id FROM model_catalog c JOIN task_presets p ON p.base_model_repo = c.repo_id LIMIT 1")
            assert await conn.fetchval(service._DELETE_UNUSED, model_id) is None
            assert await conn.fetchval(service._EXISTS, model_id) is True

    two_tenants.run(pinned())


@pytest.mark.parametrize("sql, args", [
    (INSERT, (SHA, "maybe")),  # status CHECK
    (INSERT, ("main", CatalogStatus.ENABLED)),  # revision CHECK: a commit sha, not a branch
])
def test_the_checks_hold(two_tenants, sql, args):
    async def bad():
        async with two_tenants.app_role() as conn:
            await conn.fetchval(sql, *args)

    with pytest.raises(asyncpg.CheckViolationError):
        two_tenants.run(bad())


def test_the_same_model_twice_for_a_task_is_refused(two_tenants):
    async def twice():
        async with two_tenants.app_role() as conn:
            await conn.fetchval(INSERT, SHA, CatalogStatus.ENABLED)
            await conn.fetchval(INSERT, SHA, CatalogStatus.ENABLED)

    with pytest.raises(asyncpg.UniqueViolationError):
        two_tenants.run(twice())
