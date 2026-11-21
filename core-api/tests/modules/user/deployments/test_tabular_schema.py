"""A tabular deployment's call shape: one number field per feature column the model was trained on, in its order."""
import asyncio
import uuid
from types import SimpleNamespace

from app.core import task_types
from app.core.enums import TaskType
from app.modules.user.deployments import service
from tests.support import FakeConn, patch_tenant_conn

CTX = SimpleNamespace(tenant_id=str(uuid.uuid4()), actor="user-1")
PROJECT, DEPLOYMENT = uuid.uuid4(), uuid.uuid4()


def info(encoder, postprocess):
    return SimpleNamespace(encoder=encoder, postprocess=postprocess)


def schema_for(monkeypatch, *, encoder, postprocess, features, categories=None):
    row = {"model_id": uuid.uuid4(), "mlflow_version": "3", "features": features, "categories": categories}
    patch_tenant_conn(monkeypatch, FakeConn(row=row), service)

    async def task(conn, project_id):
        return TaskType.TABULAR_CLASSIFICATION

    async def get(conn, t):
        return info(encoder, postprocess)

    monkeypatch.setattr(service, "get_task_type", task)
    monkeypatch.setattr(task_types, "get", get)
    return asyncio.run(service.get_schema(CTX, PROJECT, DEPLOYMENT, "https://api.example"))


def test_the_input_is_one_number_per_feature_column_in_the_models_order(monkeypatch):
    out = schema_for(monkeypatch, encoder="tabular_encoder", postprocess="ranked_probabilities", features=["income", "age"])
    assert list(out.input) == ["income", "age"] and set(out.input.values()) == {"number"}
    assert out.output == {"predictions": [{"class": "string", "probability": "number"}]}


def test_a_model_without_recorded_features_has_no_input_fields(monkeypatch):
    out = schema_for(monkeypatch, encoder="tabular_encoder", postprocess="ranked_probabilities", features=None)
    assert out.input == {}


def test_a_category_column_takes_text_and_a_regression_answers_a_value(monkeypatch):
    out = schema_for(monkeypatch, encoder="tabular_encoder", postprocess="regress", features=["age", "colour"], categories={"colour": ["blue", "red"]})
    assert out.input == {"age": "number", "colour": "string"} and out.output == {"predictions": [{"value": "number"}]}
    assert out.snippets  # built with the placeholders
