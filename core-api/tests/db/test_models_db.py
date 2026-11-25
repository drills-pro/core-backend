"""Models and the deployment-to-version link on a real Postgres: one live deployment per version, the model lookup behind
POST /v1/predict under RLS (tenant A's key cannot reach tenant B's model), record_version's SQL."""
import asyncpg
import pytest

from app.modules.user.deployments import model_predict
from app.modules.user.deployments.status import VERSION_LIVE_STATUSES, DeploymentStatus
from app.modules.user.model_registry import service as registry
from tests.db.test_deployments_db import rejected

LINK = (
    "UPDATE deployments d SET model_version_id = v.id, status = $2 FROM model_versions v "
    "WHERE d.training_run_id = v.training_run_id AND d.tenant_id = $1"
)
NEW_RUN = (
    "INSERT INTO training_runs (tenant_id, project_id, idempotency_key, ray_job_id, dataset_ref) "
    "SELECT tenant_id, project_id, 'k2', 'r2', 'x' FROM training_runs WHERE tenant_id = $1 LIMIT 1 RETURNING id"
)
SECOND_DEPLOYMENT = (
    "INSERT INTO deployments (tenant_id, project_id, training_run_id, route_prefix, status, model_version_id) "
    "SELECT tenant_id, project_id, $2, 'p-' || gen_random_uuid()::text, $3, model_version_id FROM deployments WHERE tenant_id = $1 LIMIT 1"
)


@pytest.mark.parametrize("held", VERSION_LIVE_STATUSES)
def test_a_version_with_a_live_deployment_cannot_get_another_on_any_run(two_tenants, held):
    t = two_tenants

    async def go():
        await t.conn.execute(LINK, t.a, held)
        run = await t.conn.fetchval(NEW_RUN, t.a)
        for new in VERSION_LIVE_STATUSES:
            with pytest.raises(asyncpg.UniqueViolationError, match="deployments_one_active_per_version"):
                async with t.conn.transaction():
                    await t.conn.execute(SECOND_DEPLOYMENT, t.a, run, new)

    t.run(go())


@pytest.mark.parametrize("released", [DeploymentStatus.FAILED, DeploymentStatus.DELETED, DeploymentStatus.STOPPED])
def test_a_deployment_that_is_not_deploying_or_active_leaves_the_version_free(two_tenants, released):
    t = two_tenants

    async def go():
        await t.conn.execute(LINK, t.a, released)
        run = await t.conn.fetchval(NEW_RUN, t.a)
        assert not await rejected(t.conn, SECOND_DEPLOYMENT, t.a, run, DeploymentStatus.ACTIVE)

    t.run(go())


def test_the_deployment_drift_check_names_the_statuses_the_index_holds(two_tenants):
    t = two_tenants

    async def go():
        where = await t.conn.fetchval("SELECT pg_get_expr(indpred, indrelid) FROM pg_index WHERE indexrelid = 'deployments_one_active_per_version'::regclass")
        assert all(f"'{s}'" in where for s in VERSION_LIVE_STATUSES) and where.count("::text") == len(VERSION_LIVE_STATUSES)

    t.run(go())


def test_tenant_a_cannot_resolve_tenant_bs_deployment_but_resolves_its_own_and_its_main(two_tenants):
    t = two_tenants

    async def go():
        for tenant in (t.a, t.b):
            await t.conn.execute(LINK, tenant, DeploymentStatus.ACTIVE)
            await t.conn.execute("UPDATE deployments SET main = true WHERE tenant_id = $1", tenant)
        deployments = {k: await t.conn.fetchrow("SELECT id, project_id FROM deployments WHERE tenant_id = $1", k) for k in (t.a, t.b)}
        async with t.as_tenant(t.a) as conn:
            own = await conn.fetchrow(model_predict._RESOLVE, deployments[t.a]["id"], deployments[t.a]["project_id"], DeploymentStatus.DELETED)
            assert own["mlflow_version"] == "1"
            # RLS hides another tenant's deployment, and so does the project filter.
            assert await conn.fetchrow(model_predict._RESOLVE, deployments[t.b]["id"], deployments[t.b]["project_id"], DeploymentStatus.DELETED) is None
            assert await conn.fetchrow(model_predict._RESOLVE, deployments[t.b]["id"], deployments[t.a]["project_id"], DeploymentStatus.DELETED) is None
            # A call with no model names the project's main deployment, which is the tenant's own.
            assert (await conn.fetchrow(model_predict._RESOLVE_MAIN, deployments[t.a]["project_id"], DeploymentStatus.DELETED))["id"] == deployments[t.a]["id"]

    t.run(go())


def test_record_version_joins_the_model_of_its_name_and_makes_one_per_name(two_tenants):
    t = two_tenants

    async def go():
        project, run, tenant = await t.conn.fetchrow("SELECT project_id, id, tenant_id::text FROM training_runs WHERE tenant_id = $1", t.a)
        async with t.as_tenant(t.a) as conn:
            for name, count in (("m", 1), ("other", 2)):
                registered = registry.RegisteredVersion(mlflow_model_name=name, mlflow_version="9")
                await registry.record_version(conn, tenant, project, run, registered)
                await registry.record_version(conn, tenant, project, run, registered)
                assert await conn.fetchval("SELECT count(*) FROM models WHERE project_id = $1", project) == count
            same = await conn.fetchval("SELECT count(DISTINCT model_id) FROM model_versions WHERE mlflow_model_name = 'other'")
            assert same == 1

    t.run(go())


def test_a_model_name_is_unique_per_project_and_an_api_key_cannot_name_a_missing_model(two_tenants):
    t = two_tenants

    async def go():
        assert await rejected(t.conn, "INSERT INTO models (tenant_id, project_id, name) SELECT tenant_id, project_id, name FROM models WHERE tenant_id = $1", t.a)
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await t.conn.execute(
                "INSERT INTO api_key_models (key_id, tenant_id, model_id) SELECT id, tenant_id, gen_random_uuid() FROM api_keys WHERE tenant_id = $1", t.a)

    t.run(go())
