"""The registry's service, called directly: register a run's model, promote through the stages, publish flags.
MLflow is a fake that fails the test if it is called while a connection is held."""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.problem import Problem
from app.integrations.data_plane import mlflow
from app.integrations.data_plane.mlflow import ModelStage
import asyncpg

from app.modules.user.model_registry import complete_promotion, fail_promotion, get_run_version, service
from app.modules.user.model_registry.constants import CUSTOMER_STAGE_LABELS, ProblemCode
from app.modules.user.training import TrainingStatus
from tests.support import FakeConn
from tests.support.counted_conn import patch_counted_conn

TENANT, PROJECT, RUN, VERSION, MODEL = str(uuid.uuid4()), uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
CTX = SimpleNamespace(tenant_id=TENANT, actor="user-1")
FULL_NAME = mlflow.registered_model_name(TENANT, str(PROJECT), "clf")
NOW = datetime.now(timezone.utc)
RUN_ROW = {"id": RUN, "status": TrainingStatus.SUCCEEDED, "mlflow_run_id": "mlflow-1", "class_names": []}
STORED = {"id": VERSION, "mlflow_model_name": FULL_NAME, "mlflow_version": "3", "stage": "None", "created_at": NOW}
BY_ID = "FROM model_versions WHERE id = $1 AND project_id = $2"  # the version lookup and the state read share their tail


def version(**kw):
    """A model_versions row as the promote queries read it."""
    return {**STORED, "model_id": MODEL, "promotion_status": None, "promotion_error": None, "promotion_error_code": None, **kw}


@pytest.fixture
def env(monkeypatch):
    class Env:
        conn = FakeConn(rows={"FROM training_runs": RUN_ROW, "INSERT INTO model_versions": STORED})
        mlflow_calls = []

    e = Env()
    e.opened = patch_counted_conn(monkeypatch, e.conn, service)

    async def register_model_version(tenant_id, project_id, name, run_id):
        assert e.opened.now == 0, "MLflow called while a connection was held"
        e.mlflow_calls.append(("register", name, run_id))
        return "3"

    async def transition_stage(tenant_id, project_id, name, version, stage):
        assert e.opened.now == 0, "MLflow called while a connection was held"
        e.mlflow_calls.append(("transition", name, version, stage))

    monkeypatch.setattr(service.data_plane, "register_model_version", register_model_version)
    monkeypatch.setattr(service.data_plane, "transition_stage", transition_stage)
    return e


def run(coro):
    return asyncio.run(coro)


def audited(env):
    return [args[3:5] for sql, args in env.conn.calls if sql.startswith("INSERT INTO audit_log")]


def test_register_records_the_mlflow_version_and_shows_the_chosen_name(env):
    out = run(service.register_run(CTX, PROJECT, RUN, "clf", "mlflow-1"))
    assert (out.id, out.mlflow_model_name, out.mlflow_version, out.stage) == (VERSION, "clf", "3", "None")
    assert env.mlflow_calls == [("register", "clf", "mlflow-1")]
    insert = next(args for sql, args in env.conn.calls if sql.startswith("INSERT INTO model_versions"))
    assert insert == (TENANT, PROJECT, RUN, FULL_NAME, "3")  # the namespaced name is what is stored
    assert env.opened.total == 1  # only the record is a transaction; MLflow is called with none held


def test_promote_to_staging_moves_mlflow_then_the_row_and_labels_the_stage_for_customers(env):
    env.conn.rows[BY_ID] = version()
    env.conn.rows["UPDATE model_versions SET stage"] = {**STORED, "stage": ModelStage.STAGING}
    out = run(service.promote(CTX, PROJECT, VERSION, ModelStage.STAGING))
    assert env.mlflow_calls == [("transition", "clf", "3", ModelStage.STAGING)]  # MLflow is addressed by the chosen name
    assert (out.mlflow_model_name, out.stage, out.customer_label) == ("clf", "Staging", CUSTOMER_STAGE_LABELS[ModelStage.STAGING])
    update = next(args for sql, args in env.conn.calls if sql.startswith("UPDATE model_versions SET stage"))
    assert update == (VERSION, ModelStage.STAGING)


def test_production_reads_as_the_version_being_served_not_as_live_or_deployed():
    label = CUSTOMER_STAGE_LABELS[ModelStage.PRODUCTION]
    assert label == "serving" and not {"live", "deployed"} & set(label.replace("/", " ").split())


def test_register_promote_and_publish_each_write_an_audit_row(env):
    run(service.register_run(CTX, PROJECT, RUN, "clf", "mlflow-1"))
    env.conn.rows[BY_ID] = version()
    env.conn.rows["UPDATE model_versions SET stage"] = {**STORED, "stage": ModelStage.STAGING}
    run(service.promote(CTX, PROJECT, VERSION, ModelStage.STAGING))
    env.conn.rows["UPDATE model_versions SET publish_metadata"] = {"id": VERSION, "publish_metadata": True, "publish_artifact": False}
    run(service.set_publish(CTX, PROJECT, VERSION, True, None))
    assert audited(env) == [("register", "model_version"), ("promote", "model_version"), ("set_publish", "model_version")]


def test_a_failed_mlflow_transition_leaves_the_row_alone(env, monkeypatch):
    env.conn.rows[BY_ID] = version()

    async def boom(*_):
        raise RuntimeError("mlflow down")

    monkeypatch.setattr(service.data_plane, "transition_stage", boom)
    with pytest.raises(RuntimeError):
        run(service.promote(CTX, PROJECT, VERSION, ModelStage.STAGING))
    assert not any(sql.startswith("UPDATE") for sql, _ in env.conn.calls)


def test_a_failed_row_write_puts_mlflow_back_on_the_old_stage(env, monkeypatch):
    env.conn.rows[BY_ID] = version(stage=ModelStage.NONE)

    async def boom(*_):
        raise RuntimeError("db down")

    monkeypatch.setattr(env.conn, "fetchrow", lambda sql, *a: boom() if sql.startswith("UPDATE") else FakeConn.fetchrow(env.conn, sql, *a))
    with pytest.raises(RuntimeError):
        run(service.promote(CTX, PROJECT, VERSION, ModelStage.STAGING))
    assert [c[3] for c in env.mlflow_calls] == [ModelStage.STAGING, ModelStage.NONE]


def test_promote_of_a_version_of_another_project_is_404(env):
    env.conn.rows[BY_ID] = None
    with pytest.raises(Problem) as e:
        run(service.promote(CTX, PROJECT, VERSION, ModelStage.STAGING))
    assert e.value.status_code == 404 and not env.mlflow_calls


def test_list_shows_chosen_names_and_customer_labels(env):
    model_id = uuid.uuid4()
    env.conn.rowset = [version(model_id=model_id, stage=ModelStage.PRODUCTION), version(model_id=model_id, stage=ModelStage.STAGING)]
    out = run(service.list_versions(CTX, PROJECT))
    assert [(v.mlflow_model_name, v.customer_label) for v in out] == [("clf", "serving"), ("clf", "candidate")]
    assert {v.model_id for v in out} == {model_id}  # what a caller sends as X-Model-Id


def test_publish_flags_are_independent_and_need_one(env):
    with pytest.raises(HTTPException) as e:
        run(service.set_publish(CTX, PROJECT, VERSION, None, None))
    assert e.value.status_code == 400
    env.conn.rows["UPDATE model_versions SET publish_metadata"] = {"id": VERSION, "publish_metadata": True, "publish_artifact": False}
    out = run(service.set_publish(CTX, PROJECT, VERSION, True, None))
    assert (out.publish_metadata, out.publish_artifact) == (True, False)


def test_the_lineage_reader_returns_the_newest_version_of_a_run(env):
    env.conn.rows["WHERE training_run_id"] = {k: STORED[k] for k in ("id", "mlflow_model_name", "mlflow_version", "stage")}
    ref = run(get_run_version(env.conn, RUN))
    assert (ref.id, ref.mlflow_model_name) == (VERSION, FULL_NAME)  # the namespaced name, as lineage always showed it
    env.conn.rows["WHERE training_run_id"] = None
    assert run(get_run_version(env.conn, RUN)) is None


def test_registering_twice_hits_the_unique_key_and_returns_the_one_row(env):
    from pathlib import Path
    run(service.register_run(CTX, PROJECT, RUN, "clf", "mlflow-1"))
    sql = next(s for s, _ in env.conn.calls if s.startswith("INSERT INTO model_versions"))
    migration = next((Path(__file__).parents[4] / "migrations").glob("*_model_version_unique.sql")).read_text()
    assert "ON CONFLICT (project_id, mlflow_model_name, mlflow_version)" in sql
    assert "UNIQUE (project_id, mlflow_model_name, mlflow_version)" in migration


# ---- promotion to Production (planning/27): asked for, settled by the worker, flipped only when the deployment is active ----

def sql_calls(env, prefix):
    return [args for sql, args in env.conn.calls if sql.startswith(prefix)]


def test_promote_to_production_marks_the_version_pending_and_moves_nothing_yet(env):
    env.conn.rows["UPDATE model_versions SET promotion_status"] = {**STORED, "promotion_status": "pending", "promotion_error": None, "promotion_error_code": None}
    out = run(service.promote(CTX, PROJECT, VERSION, ModelStage.PRODUCTION, acknowledge_risk=True))
    assert (out.promotion_status, out.stage) == ("pending", ModelStage.NONE) and not env.mlflow_calls  # the old stage stands until the deployment is active
    (args,) = sql_calls(env, "UPDATE model_versions SET promotion_status")
    assert args == (VERSION, PROJECT, "pending", "user-1", True, ModelStage.PRODUCTION)
    assert audited(env) == []  # the promote or rollback row is written when the stage flips


def test_asking_again_while_pending_or_once_production_answers_the_state_as_it_is(env):
    env.conn.rows["UPDATE model_versions SET promotion_status"] = None  # not touched: already pending, or already Production
    env.conn.rows[BY_ID] = version(promotion_status="pending")
    assert run(service.promote(CTX, PROJECT, VERSION, ModelStage.PRODUCTION)).promotion_status == "pending"
    env.conn.rows[BY_ID] = version(stage=ModelStage.PRODUCTION)
    assert run(service.promote(CTX, PROJECT, VERSION, ModelStage.PRODUCTION)).stage == ModelStage.PRODUCTION


def test_a_second_pending_promotion_of_the_same_model_is_409(env, monkeypatch):
    monkeypatch.setattr(env.conn, "fetchrow", lambda sql, *a: (_ for _ in ()).throw(asyncpg.UniqueViolationError("one pending per model")))
    with pytest.raises(Problem) as e:
        run(service.promote(CTX, PROJECT, VERSION, ModelStage.PRODUCTION))
    assert (e.value.status_code, e.value.code) == (409, ProblemCode.PROMOTION_IN_PROGRESS)


def test_the_serving_version_is_not_moved_down_on_its_own_and_a_pending_one_is_not_moved_at_all(env):
    env.conn.rows[BY_ID] = version(stage=ModelStage.PRODUCTION)
    with pytest.raises(Problem) as e:
        run(service.promote(CTX, PROJECT, VERSION, ModelStage.STAGING))
    assert (e.value.status_code, e.value.code) == (409, ProblemCode.LIVE_VERSION)
    env.conn.rows[BY_ID] = version(promotion_status="pending")
    with pytest.raises(Problem) as e:
        run(service.promote(CTX, PROJECT, VERSION, ModelStage.ARCHIVED))
    assert e.value.code == ProblemCode.PROMOTION_IN_PROGRESS and not env.mlflow_calls


OLD = uuid.uuid4()


def settle(env, replaced=(), target=None):
    env.conn.rows[BY_ID] = target or version(created_at=NOW)
    env.conn.rowset = list(replaced)
    run(complete_promotion(CTX, PROJECT, VERSION))


def old_production(created_at):
    return {"id": OLD, "mlflow_model_name": FULL_NAME, "mlflow_version": "2", "created_at": created_at}


def test_flipping_demotes_the_old_production_to_staging_so_a_model_has_one_production(env):
    settle(env, [old_production(NOW - timedelta(days=1))])
    assert env.mlflow_calls == [("transition", "clf", "2", ModelStage.STAGING), ("transition", "clf", "3", ModelStage.PRODUCTION)]
    assert sql_calls(env, "UPDATE model_versions SET stage = $3") == [(MODEL, ModelStage.PRODUCTION, ModelStage.STAGING, VERSION)]
    (promoted,) = sql_calls(env, "UPDATE model_versions SET stage = $2, promotion_status = NULL")
    assert promoted == (VERSION, ModelStage.PRODUCTION)  # and the pending mark is cleared in the same transaction
    assert audited(env) == [("promote", "model_version")]
    event = next(a for a in env.conn.executed if len(a) == 5)
    assert event[2:] == ("model.promoted", VERSION, ModelStage.PRODUCTION)


def test_promoting_a_version_older_than_the_production_one_is_a_rollback_and_audited_as_such(env):
    settle(env, [old_production(NOW + timedelta(days=1))])
    assert audited(env) == [("rollback", "model_version")]


def test_the_first_promotion_of_a_model_is_a_plain_promote(env):
    settle(env)
    assert env.mlflow_calls == [("transition", "clf", "3", ModelStage.PRODUCTION)] and audited(env) == [("promote", "model_version")]


def test_a_failure_while_flipping_in_mlflow_puts_the_old_stage_back_and_writes_nothing(env, monkeypatch):
    async def flaky(tenant_id, project_id, name, version_, stage):
        env.mlflow_calls.append((name, version_, stage))
        if (version_, stage) == ("3", ModelStage.PRODUCTION):
            raise RuntimeError("mlflow down")

    monkeypatch.setattr(service.data_plane, "transition_stage", flaky)
    with pytest.raises(RuntimeError):
        settle(env, [old_production(NOW - timedelta(days=1))])
    assert env.mlflow_calls == [("clf", "2", ModelStage.STAGING), ("clf", "3", ModelStage.PRODUCTION), ("clf", "2", ModelStage.PRODUCTION)]  # the old one is Production again
    assert not sql_calls(env, "UPDATE") and audited(env) == []


def test_a_failure_writing_the_flip_puts_both_versions_back_in_mlflow(env, monkeypatch):
    async def boom(*_):
        raise RuntimeError("db down")

    monkeypatch.setattr(env.conn, "execute", boom)
    with pytest.raises(RuntimeError):
        settle(env, [old_production(NOW - timedelta(days=1))], version(stage=ModelStage.STAGING))
    assert [c[3] for c in env.mlflow_calls] == [ModelStage.STAGING, ModelStage.PRODUCTION, ModelStage.STAGING, ModelStage.PRODUCTION]  # the new one back to Staging, the old to Production
    assert audited(env) == []


def test_a_failed_promotion_keeps_the_stage_and_shows_the_error_on_the_version(env):
    env.conn.rows["UPDATE model_versions SET promotion_status"] = {"id": VERSION}
    run(fail_promotion(CTX, PROJECT, VERSION, "too many active deployments for this plan", "too_many_deployments"))
    (args,) = sql_calls(env, "UPDATE model_versions SET promotion_status")
    assert args == (VERSION, PROJECT, "failed", "too many active deployments for this plan", "too_many_deployments", "pending") and not env.mlflow_calls
    event = next(a for a in env.conn.executed if len(a) == 5)
    assert event[2:] == ("model.promotion_failed", VERSION, "failed")
    env.conn.executed.clear()
    env.conn.rows["UPDATE model_versions SET promotion_status"] = None  # settled by someone else meanwhile: no second event
    run(fail_promotion(CTX, PROJECT, VERSION, "x"))
    assert not env.conn.executed


def test_the_promotion_statuses_are_the_check_constraint():
    from pathlib import Path
    import re

    from app.modules.user.model_registry.constants import PromotionStatus

    migration = next((Path(__file__).parents[4] / "migrations").glob("*_promote_deploys_console_predict.sql")).read_text()
    found = re.search(r"CONSTRAINT model_versions_promotion_status_check CHECK \(promotion_status IN \(([^)]*)\)\)", migration)
    assert {m.value for m in PromotionStatus} == set(re.findall(r"'([^']+)'", found.group(1)))
