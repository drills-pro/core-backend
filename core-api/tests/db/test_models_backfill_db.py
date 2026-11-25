"""20261109182100 (models, model_versions.model_id, deployments.model_version_id) on a POPULATED database.

Every other run of the migrations is on an empty one, where the backfills touch nothing. Here a scratch schema is built
from the SQL files in order up to the one before it, filled with realistic rows, then the target and the later migrations
are applied and the rows checked. The migrations name no schema, so `search_path` on one connection points them at the
scratch schema; the owner (ISOLATION_DB_DSN) creates and drops it, and nothing else is touched.
"""
import asyncio
import os
import uuid
from datetime import datetime
from pathlib import Path

import asyncpg

from tests.db.conftest import DSN_ENV

MIGRATIONS = Path(__file__).parents[2] / "migrations"
TARGET = "20261109182100"
FILES = sorted(MIGRATIONS.glob("*.sql"))
BEFORE = [f for f in FILES if f.name < TARGET]
FROM_TARGET = [f for f in FILES if f.name >= TARGET]

A, B = "aaaaaaaa-0000-0000-0000-00000000000{}", "bbbbbbbb-0000-0000-0000-00000000000{}"
TENANT_1, TENANT_2 = A.format(1), A.format(2)
P_ACTIVE, P_ARCHIVED, P_PENDING = B.format(1), B.format(2), B.format(3)  # P_ARCHIVED shares T1; P_PENDING is T2's
RUN = {n: f"cccccccc-0000-0000-0000-00000000000{n}" for n in range(1, 7)}
KEY = "dddddddd-0000-0000-0000-000000000001"
DANGLING_MODEL = "eeeeeeee-0000-0000-0000-000000000001"
# The scratch schema's constraint, not the live public one of the same name.
FK_VALIDATED = "SELECT convalidated FROM pg_constraint WHERE conname = 'api_key_models_model_fk' AND conrelid = 'api_key_models'::regclass"


def parts(path):
    up, down = path.read_text().split("-- migrate:down")
    return up.replace("-- migrate:up", ""), down


async def apply_up(conn, files):
    for f in files:
        await conn.execute(parts(f)[0])


async def seed(conn, key_rows):
    """T1 has an active and an archived project, T2 a pending_delete one; the same registered name in all three."""
    await conn.execute("INSERT INTO tenants (id, name, zitadel_org_id) VALUES ($1, 't1', 'org1'), ($2, 't2', 'org2')",
                       uuid.UUID(TENANT_1), uuid.UUID(TENANT_2))
    await conn.execute(
        "INSERT INTO projects (id, tenant_id, name, status) VALUES ($1, $2, 'p1', 'active'), ($3, $2, 'p2', 'archived'), "
        "($4, $5, 'p3', 'pending_delete')",
        uuid.UUID(P_ACTIVE), uuid.UUID(TENANT_1), uuid.UUID(P_ARCHIVED), uuid.UUID(P_PENDING), uuid.UUID(TENANT_2))
    for n, (tenant, project) in {1: (TENANT_1, P_ACTIVE), 2: (TENANT_1, P_ACTIVE), 3: (TENANT_1, P_ACTIVE), 4: (TENANT_1, P_ARCHIVED),
                                 5: (TENANT_1, P_ACTIVE), 6: (TENANT_2, P_PENDING)}.items():
        await conn.execute("INSERT INTO training_runs (id, tenant_id, project_id, idempotency_key, ray_job_id, dataset_ref) "
                           "VALUES ($1, $2, $3, $4, 'job', 'ds')", uuid.UUID(RUN[n]), uuid.UUID(tenant), uuid.UUID(project), f"k{n}")
    # (run, name, mlflow_version, created_at): R3 has three versions over two names, the newest is 'fraud' 1.
    versions = [(1, "churn", "1", "2026-01-01"), (2, "churn", "2", "2026-02-01"), (3, "churn", "3", "2026-03-01"),
                (3, "churn", "4", "2026-03-05"), (3, "fraud", "1", "2026-03-09"), (4, "churn", "1", "2026-01-15"),
                (6, "churn", "1", "2026-01-20")]
    for run, name, version, created in versions:
        tenant, project = await conn.fetchrow("SELECT tenant_id, project_id FROM training_runs WHERE id = $1", uuid.UUID(RUN[run]))
        await conn.execute("INSERT INTO model_versions (tenant_id, project_id, training_run_id, mlflow_model_name, mlflow_version, created_at) "
                           "VALUES ($1, $2, $3, $4, $5, $6)", tenant, project, uuid.UUID(RUN[run]), name, version, datetime.fromisoformat(created + "T00:00:00+00:00"))
    # R3 has an older deleted and a live deployment; R1 a stopped one; R5 has no model_versions row at all.
    for run, status, created in [(3, "deleted", "2026-04-01"), (3, "active", "2026-04-02"), (1, "stopped", "2026-04-03"),
                                 (5, "active", "2026-04-04"), (2, "stopped", "2026-04-05"), (4, "stopped", "2026-04-06")]:
        tenant, project = await conn.fetchrow("SELECT tenant_id, project_id FROM training_runs WHERE id = $1", uuid.UUID(RUN[run]))
        await conn.execute("INSERT INTO deployments (tenant_id, project_id, training_run_id, ray_job_id, route_prefix, status, created_at) "
                           "VALUES ($1, $2, $3, 'job', '/r' || $4, $5, $6)",
                           tenant, project, uuid.UUID(RUN[run]), uuid.uuid4().hex, status, datetime.fromisoformat(created + "T00:00:00+00:00"))
    if key_rows:
        await conn.execute("INSERT INTO api_keys (id, tenant_id, project_id, name, prefix, key_hash, created_by) "
                           "VALUES ($1, $2, $3, 'k', 'pre', 'hash', 'u')", uuid.UUID(KEY), uuid.UUID(TENANT_1), uuid.UUID(P_ACTIVE))
        await conn.execute("INSERT INTO api_key_models (key_id, tenant_id, model_id) VALUES ($1, $2, $3)",
                           uuid.UUID(KEY), uuid.UUID(TENANT_1), uuid.UUID(DANGLING_MODEL))


def scenario(key_rows, check):
    """Build the pre-target schema, seed it, run `check(conn)` after the target and later migrations, then undo them."""
    async def run():
        schema = "mig_" + uuid.uuid4().hex[:12]
        conn = await asyncpg.connect(os.environ[DSN_ENV])
        try:
            await conn.execute(f"CREATE SCHEMA {schema}")
            # Functions pinned to `search_path = public` would be checked against the live schema, which is further along.
            await conn.execute(f"SET search_path TO {schema}; SET check_function_bodies = off")
            await apply_up(conn, BEFORE)
            await seed(conn, key_rows)
            before = {t: await conn.fetch(f"SELECT * FROM {t} ORDER BY 1") for t in ("training_runs", "model_versions", "deployments", "api_key_models")}
            await apply_up(conn, FROM_TARGET)
            await check(conn)
            for f in reversed(FROM_TARGET):
                await conn.execute(parts(f)[1])
            for table, rows in before.items():  # the old shape comes back with every old row and column value intact
                # deploying_since is dropped by a later migration (a stamp nothing reads): its value cannot come back, the column does.
                drop = {"deploying_since"} if table == "deployments" else set()
                def keep(r):
                    return {k: v for k, v in dict(r).items() if k not in drop}
                assert [keep(r) for r in await conn.fetch(f"SELECT * FROM {table} ORDER BY 1")] == [keep(r) for r in rows], table
            assert await conn.fetchval("SELECT to_regclass('models')") is None
            assert await conn.fetchval("SELECT to_regclass('predictions')") is None
        finally:
            await conn.execute("RESET search_path")
            await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            await conn.close()
    asyncio.run(run())


def test_backfill_on_populated_data_and_down_restores_the_old_shape():
    async def check(conn):
        models = {(str(r["project_id"]), r["name"]): r for r in await conn.fetch("SELECT * FROM models")}
        # One row per (project, name): churn+fraud in p1, churn in p2 and p3 (the same name in three projects stays three models).
        assert set(models) == {(P_ACTIVE, "churn"), (P_ACTIVE, "fraud"), (P_ARCHIVED, "churn"), (P_PENDING, "churn")}
        assert {k: str(v["tenant_id"]) for k, v in models.items()} == {
            (P_ACTIVE, "churn"): TENANT_1, (P_ACTIVE, "fraud"): TENANT_1, (P_ARCHIVED, "churn"): TENANT_1, (P_PENDING, "churn"): TENANT_2}
        assert models[(P_ACTIVE, "churn")]["created_at"].date().isoformat() == "2026-01-01"  # the oldest version's date

        # Every version is linked to the model of its own project and name.
        versions = await conn.fetch("SELECT v.mlflow_model_name, v.project_id, m.name, m.project_id AS mp, m.tenant_id = v.tenant_id AS same_tenant "
                                    "FROM model_versions v JOIN models m ON m.id = v.model_id")
        assert len(versions) == 7 and all(r["name"] == r["mlflow_model_name"] and r["mp"] == r["project_id"] and r["same_tenant"] for r in versions)

        # A deployment takes the newest version of its run, or stays NULL when the run has none.
        newest = await conn.fetchval("SELECT id FROM model_versions WHERE training_run_id = $1 ORDER BY created_at DESC LIMIT 1", uuid.UUID(RUN[3]))
        by_run = {}
        for r in await conn.fetch("SELECT training_run_id, status, model_version_id FROM deployments"):
            by_run.setdefault(str(r["training_run_id"]), []).append(r)
        assert {r["model_version_id"] for r in by_run[RUN[3]]} == {newest}  # both the deleted and the active one
        assert await conn.fetchval("SELECT mlflow_model_name || mlflow_version FROM model_versions WHERE id = $1", newest) == "fraud1"
        assert by_run[RUN[5]][0]["model_version_id"] is None
        for run in (1, 2, 4):
            assert by_run[RUN[run]][0]["model_version_id"] is not None

        # Two deployments of one run share a version, but deployments_one_live_per_run (older) already lets only one of
        # them be deploying/active/stopped, so deployments_one_active_per_version cannot fail on backfilled rows.
        assert await conn.fetchval("SELECT count(*) FROM pg_indexes WHERE indexname = 'deployments_one_active_per_version' AND schemaname = current_schema()") == 1
        try:
            await conn.execute("INSERT INTO deployments (tenant_id, project_id, training_run_id, ray_job_id, route_prefix, status, model_version_id) "
                               "SELECT tenant_id, project_id, training_run_id, 'j', '/dup', 'active', model_version_id FROM deployments "
                               "WHERE training_run_id = $1 AND status = 'active'", uuid.UUID(RUN[3]))
            raise AssertionError("a second live deployment of one version was accepted")
        except asyncpg.UniqueViolationError:
            pass

        # The old key row names nothing: the constraint stays NOT VALID, the row is untouched (the key keeps its allowlist, and
        # so is not widened), and a new dangling row is refused while a real model id is accepted.
        assert await conn.fetchval(FK_VALIDATED) is False
        assert [str(r["model_id"]) for r in await conn.fetch("SELECT model_id FROM api_key_models")] == [DANGLING_MODEL]
        try:
            await conn.execute("INSERT INTO api_key_models (key_id, tenant_id, model_id) VALUES ($1, $2, gen_random_uuid())",
                               uuid.UUID(KEY), uuid.UUID(TENANT_1))
            raise AssertionError("a new dangling api_key_models row was accepted")
        except asyncpg.ForeignKeyViolationError:
            pass
        await conn.execute("INSERT INTO api_key_models (key_id, tenant_id, model_id) SELECT $1, $2, id FROM models WHERE name = 'fraud'",
                           uuid.UUID(KEY), uuid.UUID(TENANT_1))
        await conn.execute("DELETE FROM api_key_models WHERE model_id <> $1", uuid.UUID(DANGLING_MODEL))  # leave the old shape to the down check

    scenario(key_rows=True, check=check)


def test_clean_api_key_models_validate_the_constraint():
    async def check(conn):
        assert await conn.fetchval(FK_VALIDATED) is True
        assert await conn.fetchval("SELECT count(*) FROM models") == 4

    scenario(key_rows=False, check=check)
