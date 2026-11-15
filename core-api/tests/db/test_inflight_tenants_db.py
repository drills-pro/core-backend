"""The cross-tenant inflight_* sweep lists, as core_app: a tenant that is not active is left out (audit A-04)."""
import pytest

from app.core.authz import TenantStatus
from app.modules.user.datasets.constants import ImportStatus
from app.modules.user.deployments.constants import PredictionMode, PredictionStatus
from app.modules.user.deployments.status import DeploymentStatus
from app.modules.user.training.constants import TrainingStatus

# (sweep function, what puts the tenant's seeded row in flight, the in-flight status)
SWEEPS = (
    ("inflight_training_tenant_ids", "UPDATE training_runs SET status = $2 WHERE tenant_id = $1", TrainingStatus.SUBMITTED),
    ("inflight_import_tenant_ids", "UPDATE dataset_imports SET status = $2 WHERE tenant_id = $1", ImportStatus.QUEUED),
    ("inflight_deploy_tenant_ids", "UPDATE deployments SET status = $2 WHERE tenant_id = $1", DeploymentStatus.DEPLOYING),
    ("inflight_prediction_tenant_ids",
     f"UPDATE predictions SET status = $2, mode = '{PredictionMode.BACKGROUND}' WHERE tenant_id = $1", PredictionStatus.QUEUED),
)


@pytest.mark.parametrize("tenant_status", [TenantStatus.SUSPENDED, TenantStatus.PENDING_DELETE])
@pytest.mark.parametrize(("fn", "in_flight", "status"), SWEEPS)
def test_a_sweep_skips_a_tenant_that_is_not_active(two_tenants, fn, in_flight, status, tenant_status):
    t = two_tenants

    async def listed():
        async with t.app_role() as conn:
            return {r[0] for r in await conn.fetch(f"SELECT {fn}()")}

    async def go():
        for tenant in (t.a, t.b):
            await t.conn.execute(in_flight, tenant, status)
        assert {t.a, t.b} <= await listed()
        await t.conn.execute("UPDATE tenants SET status = $2 WHERE id = $1", t.a, tenant_status)
        ids = await listed()
        assert t.a not in ids and t.b in ids
        await t.conn.execute("UPDATE tenants SET status = $2 WHERE id = $1", t.a, TenantStatus.ACTIVE)
        assert t.a in await listed()  # picked up again once it is active

    t.run(go())
