"""Row-level security on a real Postgres, as core_app, on every tenant table (planning/10-security.md: "RLS tests as the
application role against real Postgres: attempt cross-tenant select, update, delete on every tenant table").

The fake connection of the other suites cannot show any of this. Each test seeds two tenants (conftest.py) and acts as
one of them through TenantConn, the way a request does.
"""
import pytest

from tests.db.schema import APP_ROLE, SEEDS

TABLES = [table for table, _ in SEEDS]


def affected(tag):
    """Rows an UPDATE or DELETE touched, from asyncpg's command tag ("UPDATE 3")."""
    return int(tag.split()[-1])


async def count(conn, table, tenant):
    return await conn.fetchval(f"SELECT count(*) FROM {table} WHERE tenant_id = $1", tenant)


@pytest.mark.parametrize("table", TABLES)
def test_a_tenant_reads_and_changes_only_its_own_rows(two_tenants, table):
    t = two_tenants

    async def matrix():
        for me, other in ((t.a, t.b), (t.b, t.a)):
            mine, theirs = await count(t.conn, table, me), await count(t.conn, table, other)  # the owner sees every row
            assert mine > 0 and theirs > 0, f"{table}: the seed left no row for one of the tenants"
            async with t.as_tenant(me) as conn:
                assert await conn.fetchval(f"SELECT count(*) FROM {table}") == mine, "select sees more than its own rows"
                assert await count(conn, table, other) == 0, "select sees the other tenant's rows"
                assert affected(await conn.execute(f"UPDATE {table} SET tenant_id = tenant_id WHERE tenant_id = $1", other)) == 0, "update"
                assert affected(await conn.execute(f"DELETE FROM {table} WHERE tenant_id = $1", other)) == 0, "delete"
                # The same UPDATE on its own rows goes through, so the zeros above come from the policy, not the statement.
                assert affected(await conn.execute(f"UPDATE {table} SET tenant_id = tenant_id WHERE tenant_id = $1", me)) == mine
            assert await count(t.conn, table, other) == theirs, "the other tenant's rows changed"

    t.run(matrix())


@pytest.mark.parametrize("table", TABLES)
def test_a_connection_with_no_tenant_sees_nothing(two_tenants, table):
    async def select():
        async with two_tenants.app_role() as conn:  # a fresh connection: app.tenant_id was never set on it
            return await conn.fetchval(f"SELECT count(*) FROM {table}")

    assert two_tenants.run(select()) == 0


def test_every_tenant_table_is_seeded(two_tenants):
    rows = two_tenants.run(two_tenants.conn.fetch(
        """SELECT c.relname, c.relrowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
              AND EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid AND a.attname = 'tenant_id' AND NOT a.attisdropped)"""))
    in_db = {row["relname"]: row["relrowsecurity"] for row in rows}
    assert set(in_db) == set(TABLES), (
        f"tests/db/schema.py is out of step with the database: no seed for {sorted(set(in_db) - set(TABLES))}, "
        f"seeded but not a tenant table {sorted(set(TABLES) - set(in_db))}"
    )
    assert all(in_db.values()), f"row-level security is off on {sorted(table for table, on in in_db.items() if not on)}"


def test_the_app_role_is_bound_by_rls(two_tenants):
    role = two_tenants.run(two_tenants.conn.fetchrow(
        """SELECT rolsuper, rolbypassrls,
                  (SELECT count(*) FROM pg_tables WHERE schemaname = 'public' AND tableowner = $1) AS owned_tables
             FROM pg_roles WHERE rolname = $1""", APP_ROLE))
    assert not role["rolsuper"] and not role["rolbypassrls"] and role["owned_tables"] == 0
