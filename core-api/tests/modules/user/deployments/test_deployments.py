"""Deployments routes: role -> action, 403 names the action, hidden/foreign projects 404, predict stays public."""
import uuid
from datetime import datetime, timezone

import pytest

from app.core.authz import Action, Role, Visibility
from app.modules.user.deployments import lineage as lineage_module, routes, service
from tests.support import PROJECT, FakeConn, access, make_client, patch_tenant_conn

DEPLOYMENT = uuid.uuid4()
DATASET, DATASET_VERSION = uuid.uuid4(), uuid.uuid4()
BASE = f"/v1/projects/{PROJECT}/deployments"


@pytest.fixture
def call(monkeypatch, dev_headers):
    client = make_client(routes.router, raise_server_exceptions=False)

    def run(method, path, row, **kw):
        patch_tenant_conn(monkeypatch, FakeConn(row, row=row), service)
        return getattr(client, method)(path, **kw)

    return run


def test_viewer_reads_list(call):
    assert call("get", BASE, access()).status_code == 200


def test_viewer_cannot_try_403_names_action(call):
    r = call("post", f"{BASE}/{DEPLOYMENT}/test", access(), json={})
    assert (r.status_code, r.json()["action"]) == (403, Action.DEPLOYMENT_TRY)


def test_viewer_cannot_manage_403_names_action(call):
    r = call("post", f"{BASE}?training_run_id={uuid.uuid4()}", access())
    assert (r.status_code, r.json()["action"]) == (403, Action.DEPLOYMENT_MANAGE)


def test_editor_can_try_but_not_manage(call):
    row = access(roles=[Role.EDITOR])
    assert call("post", f"{BASE}/{DEPLOYMENT}/test", row, json={}).status_code != 403
    assert call("post", f"{BASE}?training_run_id={uuid.uuid4()}", row).status_code == 403


def test_deployer_can_manage(call):
    r = call("post", f"{BASE}?training_run_id={uuid.uuid4()}", access(roles=[Role.DEPLOYER]))
    assert r.status_code != 403


def test_private_unbound_and_foreign_project_are_404(call):
    assert call("get", BASE, access(visibility=Visibility.PRIVATE)).status_code == 404
    assert call("get", BASE, access(visibility=None)).status_code == 404  # project not in this tenant


def test_predict_is_public_not_session_authenticated(call):
    # reaches the X-Predict-Key check (422 without it), never the tenant/authz gate
    assert call("post", f"/v1/predict/{DEPLOYMENT}", access(), json={}).status_code == 422


def test_get_is_read_only_and_hides_internals(call, monkeypatch):
    detail = {
        "id": DEPLOYMENT, "project_id": PROJECT, "training_run_id": uuid.uuid4(), "status": "active",
        "route_prefix": "/p", "rate_limit_per_minute": 10, "created_at": datetime.now(timezone.utc),
    }
    conn = FakeConn(access(), row=detail)
    patch_tenant_conn(monkeypatch, conn, service)
    c = make_client(routes.router, raise_server_exceptions=False)
    assert c.get(f"{BASE}/{DEPLOYMENT}").status_code == 200
    sql = " ".join(q for q, _ in conn.calls).upper()
    assert "UPDATE" not in sql and "PREDICT_KEY_HASH" not in sql

RUN, VERSION = uuid.uuid4(), uuid.uuid4()
LINEAGE = f"/v1/projects/{PROJECT}/training-runs/{RUN}/lineage"
RUN_ROW = {
    "id": RUN, "status": "succeeded", "dataset_id": None, "dataset_version": "c0ffee", "dataset_version_id": DATASET_VERSION, "test_dataset_id": None,
    "test_dataset_version": None, "mlflow_run_id": "m1", "reproduction_group_id": None, "config": {},
    "created_at": datetime.now(timezone.utc),
}
DATASET_VERSION_ROW = {"id": DATASET_VERSION, "dataset_id": DATASET, "number": 3, "created_at": datetime.now(timezone.utc)}
MODEL_ROW = {"id": VERSION, "mlflow_model_name": "t.p.model", "mlflow_version": "2", "stage": "Staging"}
DEPLOYMENT_ROW = {"id": DEPLOYMENT, "status": "active", "route_prefix": "/predict/x"}


def lineage(monkeypatch, dev_headers, rows, url=LINEAGE):
    conn = FakeConn(access(), rows=rows)
    patch_tenant_conn(monkeypatch, conn, lineage_module)
    return make_client(routes.router, raise_server_exceptions=False).get(url), conn


def test_lineage_joins_the_run_its_model_and_its_deployment(monkeypatch, dev_headers):
    rows = {"FROM training_runs": RUN_ROW, "FROM dataset_versions": DATASET_VERSION_ROW, "FROM model_versions": MODEL_ROW, "FROM deployments": DEPLOYMENT_ROW}
    r, _ = lineage(monkeypatch, dev_headers, rows)
    body = r.json()
    assert r.status_code == 200
    assert (body["training_run"]["id"], body["model_version"]["stage"], body["deployment"]["route_prefix"]) == (str(RUN), "Staging", "/predict/x")
    assert (body["dataset_version"]["id"], body["dataset_version"]["number"]) == (str(DATASET_VERSION), 3)


def test_the_model_card_carries_the_scores_the_dataset_version_and_the_test_split_flag(monkeypatch, dev_headers):
    run = {**RUN_ROW, "task": "image_classification", "preset": "small", "metrics": {"top1": 0.9},
           "base_model": {"repo_id": "org/vit", "revision": "abc"}}
    rows = {"FROM training_runs": run, "FROM dataset_versions": DATASET_VERSION_ROW, "FROM model_versions": MODEL_ROW, "FROM deployments": None}
    r, _ = lineage(monkeypatch, dev_headers, rows, f"/v1/projects/{PROJECT}/training-runs/{RUN}/card")
    body = r.json()
    assert r.status_code == 200 and (body["metrics"], body["task"], body["preset"]) == ({"top1": 0.9}, "image_classification", "small")
    assert body["base_model"] == {"repo_id": "org/vit", "revision": "abc"}
    assert (body["has_test_split"], body["dataset_version"]["number"], body["model_version"]["stage"]) == (False, 3, "Staging")
    r, _ = lineage(monkeypatch, dev_headers, {"FROM training_runs": {**run, "test_dataset_id": uuid.uuid4()}}, f"/v1/projects/{PROJECT}/training-runs/{RUN}/card")
    assert r.json()["has_test_split"] is True


def test_the_card_of_an_unknown_run_is_404(monkeypatch, dev_headers):
    r, _ = lineage(monkeypatch, dev_headers, {"FROM training_runs": None}, f"/v1/projects/{PROJECT}/training-runs/{RUN}/card")
    assert r.status_code == 404


def test_lineage_of_a_run_without_a_dataset_version_skips_the_version_lookup(monkeypatch, dev_headers):
    r, conn = lineage(monkeypatch, dev_headers, {"FROM training_runs": {**RUN_ROW, "dataset_version_id": None}, "FROM model_versions": None})
    assert r.json()["dataset_version"] is None
    assert not any("FROM dataset_versions" in sql for sql, _ in conn.calls)


def test_lineage_of_a_run_without_a_model_has_no_deployment_lookup(monkeypatch, dev_headers):
    r, conn = lineage(monkeypatch, dev_headers, {"FROM training_runs": RUN_ROW, "FROM model_versions": None})
    assert (r.json()["model_version"], r.json()["deployment"]) == (None, None)
    assert not any("FROM deployments" in sql for sql, _ in conn.calls)


def test_lineage_of_an_unknown_run_is_404(monkeypatch, dev_headers):
    r, _ = lineage(monkeypatch, dev_headers, {"FROM training_runs": None})
    assert r.status_code == 404
