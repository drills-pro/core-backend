"""GET .../deployments/{id}/schema: the input and output shape from the task type's functions, and snippets that never hold a key."""
import uuid
from types import SimpleNamespace

from app.modules.user.deployments import routes, service
from app.modules.user.deployments.constants import SNIPPET_KEY_PLACEHOLDER
from tests.support import PROJECT, FakeConn, access, make_client, patch_tenant_conn

DEPLOYMENT = uuid.uuid4()
MODEL = uuid.uuid4()
URL = f"/v1/projects/{PROJECT}/deployments/{DEPLOYMENT}/schema"


def call(monkeypatch, dev_headers, encoder, postprocess, row=True):
    conn = FakeConn(access(), row={"model_id": MODEL, "mlflow_version": "3"} if row else None)
    patch_tenant_conn(monkeypatch, conn, service)

    async def task(c, project_id):
        return "object-detection"

    async def info(c, slug):
        return SimpleNamespace(encoder=encoder, postprocess=postprocess)

    monkeypatch.setattr(service, "get_task_type", task)
    monkeypatch.setattr(service.task_types, "get", info)
    return make_client(routes.router).get(URL)


def test_an_image_detector_documents_its_input_boxes_and_snippets(monkeypatch, dev_headers):
    r = call(monkeypatch, dev_headers, "image_encoder", "detect")
    body = r.json()
    assert r.status_code == 200 and body["input"] == {"image_base64": "string"}
    assert "box" in body["output"]["predictions"][0] and body["deployment_id"] == str(DEPLOYMENT)
    for text in body["snippets"].values():
        assert SNIPPET_KEY_PLACEHOLDER in text and str(DEPLOYMENT) in text and "/v1/predict" in text


def test_a_text_model_takes_text(monkeypatch, dev_headers):
    assert call(monkeypatch, dev_headers, "text_encoder", "classify").json()["input"] == {"text": "string"}


def test_a_missing_deployment_is_404(monkeypatch, dev_headers):
    assert call(monkeypatch, dev_headers, "image_encoder", "detect", row=False).status_code == 404
