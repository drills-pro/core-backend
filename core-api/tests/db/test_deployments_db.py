"""The deployment indexes on a real Postgres: one live deployment per training run, one deployment per Idempotency-Key."""
import asyncpg
import pytest

from app.modules.user.deployments.status import LIVE_STATUSES, DeploymentStatus

INSERT = (
    "INSERT INTO deployments (tenant_id, project_id, training_run_id, route_prefix, status, idempotency_key) "
    "SELECT tenant_id, project_id, training_run_id, 'p-' || gen_random_uuid()::text, $2, $3 FROM deployments WHERE tenant_id = $1 LIMIT 1"
)
SET_STATUS = "UPDATE deployments SET status = $2 WHERE tenant_id = $1"


async def rejected(conn, sql, *args):
    """True when the statement hits a unique index (in a savepoint, so the transaction goes on)."""
    try:
        async with conn.transaction():
            await conn.execute(sql, *args)
    except asyncpg.UniqueViolationError:
        return True
    return False


@pytest.mark.parametrize("held", LIVE_STATUSES)
def test_a_run_with_a_live_deployment_cannot_get_another(two_tenants, held):
    t = two_tenants

    async def go():
        await t.conn.execute(SET_STATUS, t.a, held)
        for new in LIVE_STATUSES:
            assert await rejected(t.conn, INSERT, t.a, new, None), f"{new} next to {held}"

    t.run(go())


@pytest.mark.parametrize("released", [DeploymentStatus.FAILED, DeploymentStatus.DELETED])
def test_a_failed_or_deleted_deployment_releases_its_run(two_tenants, released):
    t = two_tenants

    async def go():
        await t.conn.execute(SET_STATUS, t.a, released)
        assert not await rejected(t.conn, INSERT, t.a, DeploymentStatus.ACTIVE, None)
        assert await rejected(t.conn, INSERT, t.a, DeploymentStatus.ACTIVE, None)  # and now the new one holds it

    t.run(go())


def test_moving_a_deployment_onto_a_held_run_is_rejected_too(two_tenants):
    """The `main` re-deploy updates the row's training_run_id in place."""
    t = two_tenants

    async def go():
        await t.conn.execute(SET_STATUS, t.a, DeploymentStatus.FAILED)
        await t.conn.execute(INSERT, t.a, DeploymentStatus.ACTIVE, None)  # holds the run
        await t.conn.execute(
            "INSERT INTO training_runs (tenant_id, project_id, idempotency_key, ray_job_id, dataset_ref) "
            "SELECT tenant_id, project_id, 'k2', 'r2', 'x' FROM training_runs WHERE tenant_id = $1 LIMIT 1", t.a)
        other = await t.conn.fetchval("SELECT id FROM training_runs WHERE tenant_id = $1 AND idempotency_key = 'k2'", t.a)
        await t.conn.execute(
            "INSERT INTO deployments (tenant_id, project_id, training_run_id, route_prefix) "
            "SELECT tenant_id, id, $2, 'p-main' FROM projects WHERE tenant_id = $1 LIMIT 1", t.a, other)
        held = await t.conn.fetchval("SELECT training_run_id FROM deployments WHERE tenant_id = $1 AND status = 'active'", t.a)
        assert await rejected(t.conn, "UPDATE deployments SET training_run_id = $2 WHERE route_prefix = 'p-main' AND tenant_id = $1", t.a, held)

    t.run(go())


def test_an_idempotency_key_is_taken_once_per_project(two_tenants):
    t = two_tenants

    async def go():
        await t.conn.execute(SET_STATUS, t.a, DeploymentStatus.FAILED)  # frees the run, the key is what is tested
        await t.conn.execute(INSERT, t.a, DeploymentStatus.FAILED, "k")
        assert await rejected(t.conn, INSERT, t.a, DeploymentStatus.FAILED, "k")
        assert not await rejected(t.conn, INSERT, t.a, DeploymentStatus.FAILED, None)  # old rows have no key: any number
        assert not await rejected(t.conn, INSERT, t.a, DeploymentStatus.FAILED, None)

    t.run(go())
