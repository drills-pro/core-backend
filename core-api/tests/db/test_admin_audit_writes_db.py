"""A wired admin write on a real Postgres as core_app: its admin_audit row lands in the same transaction as the change."""
import json
from types import SimpleNamespace

import pytest

from app.core.enums import AdminAuditAction, AdminAuditEntity
from app.core.pagination import PageParams
from app.modules.admin.model_catalog import service
from app.modules.admin.model_catalog.schemas import CatalogModelCreate
from tests.support import TenantCtx

ACTOR = "ops-wired"
BODY = CatalogModelCreate(repo_id="org/audited", revision="c" * 40, display_name="audited", task="image-classification", license="apache-2.0")
ROWS = "SELECT action, entity_type, entity_id FROM admin_audit WHERE actor = $1 ORDER BY id"


@pytest.fixture
def as_app(two_tenants, monkeypatch):
    two_tenants.run(two_tenants.conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"))

    async def get_pool():
        return SimpleNamespace(acquire=lambda: TenantCtx(two_tenants.conn))

    monkeypatch.setattr(service, "get_pool", get_pool)
    return two_tenants


def test_a_catalog_model_and_its_audit_rows_are_written_together(as_app):
    async def lifecycle():
        async with as_app.app_role() as conn:
            created = await service.create(BODY, ACTOR)
            await service.set_status(created.id, "disabled", ACTOR)
            await service.delete(created.id, ACTOR)
            return created.id, [tuple(r) for r in await conn.fetch(ROWS, ACTOR)]

    model_id, rows = as_app.run(lifecycle())
    assert rows == [
        (AdminAuditAction.MODEL_CATALOG_CREATE, AdminAuditEntity.MODEL_CATALOG, str(model_id)),
        (AdminAuditAction.MODEL_CATALOG_DISABLE, AdminAuditEntity.MODEL_CATALOG, str(model_id)),
        (AdminAuditAction.MODEL_CATALOG_DELETE, AdminAuditEntity.MODEL_CATALOG, str(model_id)),
    ]


def test_the_audit_log_lists_what_was_written_filtered_and_counted(as_app, monkeypatch):
    from app.modules.admin.audit_log import service as log

    async def get_pool():
        return SimpleNamespace(acquire=lambda: TenantCtx(as_app.conn))

    monkeypatch.setattr(log, "get_pool", get_pool)

    async def read():
        async with as_app.app_role():
            created = await service.create(BODY, ACTOR)
            await service.set_status(created.id, "disabled", ACTOR)
            mine, total = await log.list_entries(AdminAuditEntity.MODEL_CATALOG, ACTOR, PageParams(page_size=1, page_token=None))
            other, other_total = await log.list_entries(AdminAuditEntity.TENANT, ACTOR, PageParams(page_size=10, page_token=None))
            return [e.action for e in mine.items], total, other.items, other_total

    actions, total, none, other_total = as_app.run(read())
    assert (actions, total, none, other_total) == ([AdminAuditAction.MODEL_CATALOG_DISABLE], 2, [], 0)


def test_a_refused_write_leaves_no_audit_row(as_app):
    async def twice():
        async with as_app.app_role() as conn:
            await service.create(BODY, ACTOR)
            with pytest.raises(Exception, match="already listed"):
                await service.create(BODY, ACTOR)
            return await conn.fetchval("SELECT count(*) FROM admin_audit WHERE actor = $1", ACTOR)

    assert as_app.run(twice()) == 1
