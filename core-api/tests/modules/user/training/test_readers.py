"""What training offers other contexts (run model facts) and the lineage facts."""
import asyncio
import uuid
from datetime import UTC, datetime


from app.modules.user.training import RunModel, TrainingStatus, get_run_lineage, get_run_model
from tests.support import PROJECT, FakeConn

RUN = uuid.uuid4()
S = TrainingStatus
RUN_ROW = {
    "id": RUN, "status": S.SUCCEEDED, "dataset_id": None, "dataset_version": "c0ffee", "dataset_version_id": None, "test_dataset_id": None, "test_dataset_version": None,
    "mlflow_run_id": "mlflow-1", "reproduction_group_id": None, "config": {}, "created_at": datetime.now(UTC),
}


def model(**kw):
    return RunModel(**{"id": RUN, "status": S.SUCCEEDED, "mlflow_run_id": "mlflow-1", "class_names": ["a"], **kw})


def test_a_model_exists_for_a_succeeded_run_with_an_mlflow_run_and_serves_when_it_has_class_names():
    assert model().has_model and model().servable
    assert not model(status=S.RUNNING).has_model and not model(status=S.FAILED).servable
    assert not model(mlflow_run_id=None).has_model
    assert model(class_names=[]).has_model and not model(class_names=[]).servable


def test_get_run_model_reads_the_runs_facts_and_is_none_for_another_projects_run():
    conn = FakeConn(row={"id": RUN, "status": "succeeded", "mlflow_run_id": "mlflow-1", "class_names": ["cat", "dog"]})
    out = asyncio.run(get_run_model(conn, PROJECT, RUN))
    assert (out.status, out.mlflow_run_id, out.class_names) == (S.SUCCEEDED, "mlflow-1", ["cat", "dog"])
    assert conn.calls[0][1] == (RUN, PROJECT)
    assert asyncio.run(get_run_model(FakeConn(row=None), PROJECT, RUN)) is None


def test_get_run_lineage_reads_the_run_and_is_none_for_another_projects_run():
    conn = FakeConn(row=RUN_ROW)
    assert asyncio.run(get_run_lineage(conn, PROJECT, RUN)).id == RUN
    assert conn.calls[0][1] == (RUN, PROJECT)
    assert asyncio.run(get_run_lineage(FakeConn(row=None), PROJECT, RUN)) is None
