"""The dataset queries as core_app on a real Postgres, against the migrated schema. datasets.deleted_at was dropped by a
migration, and the queries that still named it passed every fake-connection test and failed only in the database."""
import json

import pytest

from app.core.authz import Action, AuthzContext, TenantRole
from app.core.pagination import PageParams
from app.modules.user.datasets import import_service, readers, service


def _ctx(tenant) -> AuthzContext:
    return AuthzContext(str(tenant), "u1", TenantRole.MEMBER, None, frozenset(Action))


@pytest.mark.parametrize("sql", [readers._LIVE_DATASET, readers._VERSION_SOURCE, import_service._LOCK_DATASET, import_service._IMPORT])
def test_every_dataset_query_prepares_on_the_migrated_schema(two_tenants, sql):
    two_tenants.run(two_tenants.conn.prepare(sql))


def test_the_dataset_reads_run_as_core_app(two_tenants):
    t = two_tenants

    async def go():
        await t.conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")  # the app's pool sets it too
        project = await t.conn.fetchval("SELECT id FROM projects WHERE tenant_id = $1", t.a)
        dataset = await t.conn.fetchval("SELECT id FROM datasets WHERE tenant_id = $1", t.a)
        version = await t.conn.fetchval("SELECT id FROM dataset_versions WHERE tenant_id = $1", t.a)
        async with t.app_role():
            page = await service.list_datasets(_ctx(t.a), project, PageParams(page_size=10, page_token=None))
            assert [d.id for d in page.items] == [dataset]
            assert (await service.get_dataset(_ctx(t.a), project, dataset)).id == dataset
            versions = await service.list_versions(_ctx(t.a), project, dataset, PageParams(page_size=10, page_token=None))
            assert [v.id for v in versions.items] == [version]

    t.run(go())
