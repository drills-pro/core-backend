"""Two replicas claiming at once never take the same prediction: FOR UPDATE SKIP LOCKED on real, committed rows and two connections as core_app.
The rows are committed (a lock needs another transaction to see the data), so the test removes its tenant when it is done."""
import asyncio
import json
import os
import uuid

import asyncpg

from app.modules.user.deployments import prediction_store as store
from app.modules.user.deployments.constants import PredictionStatus
from tests.db.conftest import DSN_ENV
from tests.db.schema import APP_ROLE, SEEDS, TENANT_SEED

ROWS = 12
CLONE = (  # one more background prediction like the seeded one
    "INSERT INTO predictions (tenant_id, project_id, api_key_id, deployment_id, model_id, model_revision, mode, status, max_attempts) "
    "SELECT tenant_id, project_id, api_key_id, deployment_id, model_id, model_revision, mode, status, max_attempts FROM predictions "
    "WHERE tenant_id = $1 LIMIT 1"
)


async def as_core_app(conn, tenant):
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")  # as app/core/db.py does for the pool
    await conn.execute("BEGIN")
    await conn.execute(f"SET LOCAL ROLE {APP_ROLE}")
    await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant))


def test_two_connections_claiming_at_once_get_disjoint_rows():
    async def go():
        owner = await asyncpg.connect(os.environ[DSN_ENV])
        tenant = uuid.uuid4()
        try:
            await owner.execute(TENANT_SEED, tenant)
            for _, sql in SEEDS:
                await owner.execute(sql, tenant)
            for _ in range(ROWS - 1):
                await owner.execute(CLONE, tenant)
            a, b = await asyncpg.connect(os.environ[DSN_ENV]), await asyncpg.connect(os.environ[DSN_ENV])
            try:
                await as_core_app(a, tenant)
                await as_core_app(b, tenant)
                first = await store.claim(a, str(tenant), ROWS // 2, "a")  # a holds its row locks until it commits
                second = await store.claim(b, str(tenant), ROWS, "b")  # b asks for everything and skips what a holds
                assert len(first) == ROWS // 2 and len(second) == ROWS - ROWS // 2
                assert {r["id"] for r in first}.isdisjoint(r["id"] for r in second)
                await a.execute("COMMIT")
                await b.execute("COMMIT")
            finally:
                await asyncio.gather(a.close(), b.close())
            counts = await owner.fetchrow(
                "SELECT count(*) FILTER (WHERE lease_owner = 'a') AS a, count(*) FILTER (WHERE lease_owner = 'b') AS b, "
                "count(*) FILTER (WHERE status = $2 AND attempt = 1) AS claimed FROM predictions WHERE tenant_id = $1", tenant, PredictionStatus.IN_PROGRESS)
            assert (counts["a"], counts["b"], counts["claimed"]) == (ROWS // 2, ROWS - ROWS // 2, ROWS)
        finally:
            for table, _ in reversed(SEEDS):
                await owner.execute(f"DELETE FROM {table} WHERE tenant_id = $1", tenant)  # noqa: S608 - the names are the schema's own
            await owner.execute("DELETE FROM tenants WHERE id = $1", tenant)
            await owner.close()

    asyncio.run(go())
