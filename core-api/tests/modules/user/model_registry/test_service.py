"""The registry's service, called directly: register a run's model, promote through the stages, publish flags.
MLflow is a fake that fails the test if it is called while a connection is held."""
import asyncio
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.problem import Problem
from app.integrations.data_plane import mlflow
from app.integrations.data_plane.mlflow import ModelStage
from app.modules.user.model_registry import get_run_version, service
from app.modules.user.model_registry.constants import CUSTOMER_STAGE_LABELS
from app.modules.user.training import TrainingStatus
from tests.support import FakeConn
from tests.support.counted_conn import patch_counted_conn

TENANT, PROJECT, RUN, VERSION = str(uuid.uuid4()), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
CTX = SimpleNamespace(tenant_id=TENANT)
FULL_NAME = mlflow.registered_model_name(TENANT, str(PROJECT), "clf")
NOW = datetime.now(timezone.utc)
RUN_ROW = {"id": RUN, "status": TrainingStatus.SUCCEEDED, "mlflow_run_id": "mlflow-1", "class_names": []}
STORED = {"id": VERSION, "mlflow_model_name": FULL_NAME, "mlflow_version": "3", "stage": "None", "created_at": NOW}


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

    monkeypatch.setattr(service.mlflow, "register_model_version", register_model_version)
    monkeypatch.setattr(service.mlflow, "transition_stage", transition_stage)
    return e


def run(coro):
    return asyncio.run(coro)


def test_register_records_the_mlflow_version_and_shows_the_chosen_name(env):
    out = run(service.register_run(CTX, PROJECT, RUN, "clf", "mlflow-1"))
    assert (out.id, out.mlflow_model_name, out.mlflow_version, out.stage) == (VERSION, "clf", "3", "None")
    assert env.mlflow_calls == [("register", "clf", "mlflow-1")]
    insert = next(args for sql, args in env.conn.calls if sql.startswith("INSERT INTO model_versions"))
    assert insert == (TENANT, PROJECT, RUN, FULL_NAME, "3")  # the namespaced name is what is stored
    assert env.opened.total == 1  # only the record is a transaction; MLflow is called with none held


def test_promote_moves_mlflow_then_the_row_and_labels_the_stage_for_customers(env):
    env.conn.rows["SELECT mlflow_model_name, mlflow_version"] = {"mlflow_model_name": FULL_NAME, "mlflow_version": "3"}
    env.conn.rows["UPDATE model_versions SET stage"] = {**STORED, "stage": ModelStage.STAGING}
    out = run(service.promote(CTX, PROJECT, VERSION, ModelStage.STAGING))
    assert env.mlflow_calls == [("transition", "clf", "3", ModelStage.STAGING)]  # MLflow is addressed by the chosen name
    assert (out.mlflow_model_name, out.stage, out.customer_label) == ("clf", "Staging", CUSTOMER_STAGE_LABELS[ModelStage.STAGING])
    update = next(args for sql, args in env.conn.calls if sql.startswith("UPDATE model_versions SET stage"))
    assert update == (VERSION, ModelStage.STAGING)


def test_promote_to_production_is_also_the_rollback_of_an_older_version(env):
    env.conn.rows["SELECT mlflow_model_name, mlflow_version"] = {"mlflow_model_name": FULL_NAME, "mlflow_version": "1"}
    env.conn.rows["UPDATE model_versions SET stage"] = {**STORED, "mlflow_version": "1", "stage": ModelStage.PRODUCTION}
    out = run(service.promote(CTX, PROJECT, VERSION, ModelStage.PRODUCTION))
    assert (out.mlflow_version, out.customer_label) == ("1", "live/deployed")


def test_a_failed_mlflow_transition_leaves_the_row_alone(env, monkeypatch):
    env.conn.rows["SELECT mlflow_model_name, mlflow_version"] = {"mlflow_model_name": FULL_NAME, "mlflow_version": "3"}

    async def boom(*_):
        raise RuntimeError("mlflow down")

    monkeypatch.setattr(service.mlflow, "transition_stage", boom)
    with pytest.raises(RuntimeError):
        run(service.promote(CTX, PROJECT, VERSION, ModelStage.PRODUCTION))
    assert not any(sql.startswith("UPDATE") for sql, _ in env.conn.calls)


def test_a_failed_row_write_puts_mlflow_back_on_the_old_stage(env, monkeypatch):
    env.conn.rows["SELECT mlflow_model_name, mlflow_version"] = {"mlflow_model_name": FULL_NAME, "mlflow_version": "3", "stage": ModelStage.NONE}

    async def boom(*_):
        raise RuntimeError("db down")

    monkeypatch.setattr(env.conn, "fetchrow", lambda sql, *a: boom() if sql.startswith("UPDATE") else FakeConn.fetchrow(env.conn, sql, *a))
    with pytest.raises(RuntimeError):
        run(service.promote(CTX, PROJECT, VERSION, ModelStage.PRODUCTION))
    assert [c[3] for c in env.mlflow_calls] == [ModelStage.PRODUCTION, ModelStage.NONE]


def test_promote_of_a_version_of_another_project_is_404(env):
    env.conn.rows["SELECT mlflow_model_name, mlflow_version"] = None
    with pytest.raises(Problem) as e:
        run(service.promote(CTX, PROJECT, VERSION, ModelStage.STAGING))
    assert e.value.status_code == 404 and not env.mlflow_calls


def test_list_shows_chosen_names_and_customer_labels(env):
    model_id = uuid.uuid4()
    env.conn.rowset = [{**STORED, "model_id": model_id, "stage": ModelStage.PRODUCTION}, {**STORED, "model_id": model_id, "stage": ModelStage.STAGING}]
    out = run(service.list_versions(CTX, PROJECT))
    assert [(v.mlflow_model_name, v.customer_label) for v in out] == [("clf", "live/deployed"), ("clf", "candidate")]
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
