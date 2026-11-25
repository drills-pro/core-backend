"""The admin model views on a real Postgres: a deployment's status reads as the model's status."""
import pytest

from app.modules.admin.models.constants import ModelStatus
from app.modules.user.deployments.status import DeploymentStatus

EXPECTED = {
    DeploymentStatus.ACTIVE: ModelStatus.DEPLOYED,
    DeploymentStatus.STOPPED: ModelStatus.STOPPED,
    DeploymentStatus.DELETED: ModelStatus.STOPPED,
}


@pytest.mark.parametrize("deployment", EXPECTED)
def test_every_deployment_status_maps_to_a_model_status(two_tenants, deployment):
    t = two_tenants

    async def go():
        await t.conn.execute("UPDATE deployments SET status = $2 WHERE tenant_id = $1", t.a, deployment)
        rows = await t.conn.fetch("SELECT status FROM admin_models(10, NULL, NULL, $1, NULL, NULL, NULL, NULL)", t.a)
        assert [r["status"] for r in rows] == [EXPECTED[deployment]]
        rows = await t.conn.fetch("SELECT status FROM admin_model_versions((SELECT id FROM model_versions WHERE tenant_id = $1), 10, NULL, NULL)", t.a)
        assert [r["status"] for r in rows] == [EXPECTED[deployment]]
        stats = await t.conn.fetchrow("SELECT * FROM admin_model_stats()")
        assert stats["stopped_count"] >= (EXPECTED[deployment] == ModelStatus.STOPPED)

    t.run(go())
