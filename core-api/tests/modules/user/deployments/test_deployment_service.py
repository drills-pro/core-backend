"""Deployment service rules, called directly (no HTTP)."""
import uuid
from datetime import datetime, timezone

import asyncio

import pytest
from fastapi import HTTPException

from types import SimpleNamespace
from app.core.enums import Split, TaskType
from app.modules.user.deployments import service
from app.modules.user.deployments.constants import (
    METRIC_FLOORS,
    PLAN_TIER_RATE_LIMITS,
    PlanTier,
    ProblemCode,
)
from app.modules.user.deployments.status import DEPLOYMENTS, DeploymentStatus
from app.modules.user.training import TrainingStatus
from tests.support import FakeConn
from tests.support.counted_conn import patch_counted_conn

CTX = SimpleNamespace(tenant_id=str(uuid.uuid4()), actor="user-1")


def test_rate_limit_follows_plan_tier_and_unknown_falls_back_to_free():
    assert service.rate_limit_for(PlanTier.PRO) == PLAN_TIER_RATE_LIMITS[PlanTier.PRO]
    assert service.rate_limit_for("pro") == PLAN_TIER_RATE_LIMITS[PlanTier.PRO]
    assert service.rate_limit_for(None) == PLAN_TIER_RATE_LIMITS[PlanTier.FREE]


PROJECT, RUN, VERSION = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
INSERTED = {"id": uuid.uuid4(), "status": "active", "route_prefix": "/p", "rate_limit_per_minute": 10, "training_run_id": None, "model_version_id": None, "main": False}
KEY = "k"
SERVABLE = {"id": RUN, "status": TrainingStatus.SUCCEEDED, "mlflow_run_id": "mlflow-1", "class_names": ["cat", "dog"], "test_dataset_id": uuid.uuid4()}


@pytest.fixture
def deploy_env(monkeypatch):
    """create_deployment with a fake database."""
    class Env:
        conn = FakeConn(
            rows={"idempotency_key = $2": None, "FROM training_runs": SERVABLE, "INSERT INTO deployments": INSERTED},
            vals={"SELECT id FROM model_versions": VERSION, "count(*) FROM deployments": 0, "SELECT plan_tier": "pro", "SELECT task_type": TaskType.IMAGE_CLASSIFICATION},
        )

        @property
        def deployed(self):
            return [args for sql, args in self.conn.calls if sql.startswith(("INSERT INTO deployments", "UPDATE deployments"))]

    e = Env()
    e.opened = patch_counted_conn(monkeypatch, e.conn, service)
    return e


def inserted(conn):
    return next(args for sql, args in conn.calls if sql.startswith("INSERT INTO deployments"))


def test_deploy_serves_the_runs_version_and_is_not_main_unless_asked(deploy_env):
    deploy_env.conn.rows["INSERT INTO deployments"] = {"main": False,
        "id": uuid.uuid4(), "status": "active", "route_prefix": "/predict/x", "rate_limit_per_minute": PLAN_TIER_RATE_LIMITS[PlanTier.PRO],
    }
    out = asyncio.run(service.create_deployment(CTX, PROJECT, RUN, KEY))
    assert (out.status, out.plan_tier, out.rate_limit_per_minute, out.main) == ("active", "pro", PLAN_TIER_RATE_LIMITS[PlanTier.PRO], False)
    args = inserted(deploy_env.conn)
    assert args[1:6] == (CTX.tenant_id, PROJECT, RUN, None, f"/predict/{args[0]}")  # the route is the deployment's own id
    assert args[10] == VERSION and args[11] is False  # the version it serves, and no main
    assert not any(sql.startswith("UPDATE deployments SET main") for sql, _ in deploy_env.conn.calls)


def test_set_main_on_deploy_clears_the_previous_main_before_it_takes_the_slot(deploy_env):
    deploy_env.conn.rows["INSERT INTO deployments"] = {
        "id": uuid.uuid4(), "status": "active", "route_prefix": "/p", "rate_limit_per_minute": 10, "main": True,
    }
    assert asyncio.run(service.create_deployment(CTX, PROJECT, RUN, KEY, set_main=True)).main is True
    sqls = [sql for sql, _ in deploy_env.conn.calls]
    assert sqls.index("UPDATE deployments SET main = false WHERE project_id = $1 AND main") < next(i for i, s in enumerate(sqls) if s.startswith("INSERT INTO deployments"))
    assert inserted(deploy_env.conn)[11] is True


def test_set_main_moves_the_flag_to_a_live_deployment_and_audits_it(deploy_env):
    target = uuid.uuid4()
    row = {
        "id": target, "project_id": PROJECT, "training_run_id": RUN, "model_version_id": VERSION, "status": "active",
        "route_prefix": "/predict/x", "rate_limit_per_minute": 10, "created_at": datetime.now(timezone.utc), "main": True,
    }
    deploy_env.conn.rows["FROM deployments"] = row
    out = asyncio.run(service.set_main(CTX, PROJECT, target))
    assert out.main is True
    sqls = [sql for sql, _ in deploy_env.conn.calls]
    assert "UPDATE deployments SET main = false WHERE project_id = $1 AND main AND id <> $2" in sqls
    assert "UPDATE deployments SET main = true WHERE id = $1" in sqls


def test_set_main_of_a_deleted_or_unknown_deployment_is_404(deploy_env):
    deploy_env.conn.rows["FROM deployments"] = None
    with pytest.raises(HTTPException) as e:
        asyncio.run(service.set_main(CTX, PROJECT, uuid.uuid4()))
    assert e.value.status_code == 404 and not deploy_env.deployed


def test_a_run_without_a_version_is_404_before_anything_is_checked(deploy_env):
    deploy_env.conn.vals["SELECT id FROM model_versions"] = None
    with pytest.raises(HTTPException) as e:
        asyncio.run(service.create_deployment(CTX, PROJECT, RUN, KEY))
    assert e.value.status_code == 404 and not deploy_env.deployed


@pytest.mark.parametrize("row", [
    {**SERVABLE, "status": TrainingStatus.FAILED},
    {**SERVABLE, "status": TrainingStatus.RUNNING},
    {**SERVABLE, "mlflow_run_id": None},
    {**SERVABLE, "class_names": []},  # not a classification model: the serving script cannot serve it
])
def test_deploy_requires_a_succeeded_servable_run(deploy_env, row):
    deploy_env.conn.rows["FROM training_runs"] = row
    with pytest.raises(HTTPException) as e:
        asyncio.run(service.create_deployment(CTX, PROJECT, RUN, KEY))
    assert e.value.status_code == 409 and not deploy_env.deployed


def test_a_run_without_a_test_dataset_needs_acknowledge_risk_and_the_answer_is_recorded(deploy_env):
    deploy_env.conn.rows["FROM training_runs"] = {**SERVABLE, "test_dataset_id": None}
    with pytest.raises(HTTPException) as e:
        asyncio.run(service.create_deployment(CTX, PROJECT, RUN, KEY))
    assert e.value.status_code == 409 and e.value.code == ProblemCode.DEPLOY_RISK and not deploy_env.deployed
    deploy_env.conn.rows["INSERT INTO deployments"] = {"main": False, "id": uuid.uuid4(), "status": "deploying", "route_prefix": "/predict/x", "rate_limit_per_minute": 60}
    asyncio.run(service.create_deployment(CTX, PROJECT, VERSION, KEY, acknowledge_risk=True))
    assert deploy_env.deployed and inserted(deploy_env.conn)[9] is True  # risk_acknowledged


def stats(splits, row_count):
    return {"file_count": 0, "total_bytes": 0, "row_count": row_count, "splits": splits}


def test_the_gate_reads_the_test_split_from_the_runs_dataset_version_not_only_a_test_dataset(deploy_env):
    deploy_env.conn.rows["INSERT INTO deployments"] = {"main": False, "id": uuid.uuid4(), "status": "deploying", "route_prefix": "/predict/x", "rate_limit_per_minute": 60}
    no_dataset = {**SERVABLE, "test_dataset_id": None}
    for version_stats, risky in (
        (stats({Split.TRAIN: 8, Split.VALIDATION: 2}, 10), True),  # every row split, none of them test
        (stats({Split.TRAIN: 8, Split.TEST: 2}, 10), False),  # test rows the user supplied
        (stats({}, 10), False),  # rows with no split are hashed, and some of them land in test
        (None, True),  # a run from before dataset versions
    ):
        deploy_env.conn.rows["FROM training_runs"] = {**no_dataset, "version_stats": version_stats}
        if risky:
            with pytest.raises(HTTPException) as e:
                asyncio.run(service.create_deployment(CTX, PROJECT, RUN, KEY))
            assert e.value.code == ProblemCode.DEPLOY_RISK
        else:
            asyncio.run(service.create_deployment(CTX, PROJECT, RUN, KEY))
            assert inserted(deploy_env.conn)[9] is False  # no acknowledgement was needed


def test_a_score_under_the_task_floor_needs_acknowledge_risk(deploy_env):
    metric, floor = METRIC_FLOORS[TaskType.IMAGE_CLASSIFICATION]  # the fixture's default task
    deploy_env.conn.rows["FROM training_runs"] = {**SERVABLE, "metrics": {metric: floor - 0.1}}
    with pytest.raises(HTTPException) as e:
        asyncio.run(service.create_deployment(CTX, PROJECT, RUN, KEY))
    assert e.value.status_code == 409 and e.value.code == ProblemCode.DEPLOY_RISK and metric in e.value.detail and not deploy_env.deployed
    deploy_env.conn.rows["FROM training_runs"] = {**SERVABLE, "metrics": {metric: floor}}  # at the floor is fine
    deploy_env.conn.rows["INSERT INTO deployments"] = {"main": False, "id": uuid.uuid4(), "status": "deploying", "route_prefix": "/predict/x", "rate_limit_per_minute": 60}
    asyncio.run(service.create_deployment(CTX, PROJECT, RUN, KEY))
    assert deploy_env.deployed and inserted(deploy_env.conn)[9] is False


def test_a_detection_run_deploys_with_the_class_names_its_report_lists(deploy_env):
    deploy_env.conn.vals["SELECT task_type"] = TaskType.OBJECT_DETECTION
    deploy_env.conn.rows["FROM training_runs"] = {**SERVABLE, "class_names": ["person", "car"]}
    deploy_env.conn.rows["INSERT INTO deployments"] = {"main": False,
        "id": uuid.uuid4(), "status": "deploying", "route_prefix": "/predict/x", "rate_limit_per_minute": PLAN_TIER_RATE_LIMITS[PlanTier.PRO],
    }
    assert asyncio.run(service.create_deployment(CTX, PROJECT, RUN, KEY)).id
    assert inserted(deploy_env.conn)[3] == RUN  # the predictor apps read the class names from the run on the first request


@pytest.mark.parametrize("task", [None, TaskType.TABULAR_CLASSIFICATION, TaskType.TEXT_GENERATION])
def test_deploy_requires_a_servable_task_whatever_the_report_carries(deploy_env, task):
    deploy_env.conn.vals["SELECT task_type"] = task  # a tabular classifier's report has labels too
    with pytest.raises(HTTPException) as e:
        asyncio.run(service.create_deployment(CTX, PROJECT, RUN, KEY))
    assert e.value.status_code == 409 and not deploy_env.deployed


def test_deploy_of_another_projects_run_is_404(deploy_env):
    deploy_env.conn.rows["FROM training_runs"] = None
    with pytest.raises(HTTPException) as e:
        asyncio.run(service.create_deployment(CTX, PROJECT, RUN, KEY))
    assert e.value.status_code == 404 and not deploy_env.deployed


def test_every_status_is_declared_and_deleted_is_final():
    edges = DEPLOYMENTS.transitions
    assert set(edges) | {t for ts in edges.values() for t in ts} == set(DeploymentStatus)
    assert DeploymentStatus.DELETED not in edges and all(DeploymentStatus.DELETED in ts for ts in edges.values())
