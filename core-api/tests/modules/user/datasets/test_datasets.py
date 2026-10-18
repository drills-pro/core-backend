"""Authorization of the dataset, labeling and preprocessing routes, with the database faked."""
import uuid

import pytest

from app.modules.user.datasets import router as datasets
from app.modules.user.datasets import routes as dataset_routes
from app.modules.user.labeling import routes as labeling_routes
from app.modules.user.preprocessing import routes as preprocessing_routes
from tests.support import PROJECT, FakeConn, access, make_client, patch_tenant_conn

DATASET = uuid.uuid4()


@pytest.fixture
def call(monkeypatch, dev_headers):
    client = make_client(datasets, labeling_routes.router, preprocessing_routes.router)

    def send(row, method="get", path=""):
        patch_tenant_conn(monkeypatch, FakeConn(row), dataset_routes, labeling_routes, preprocessing_routes)
        return getattr(client, method)(f"/v1/projects/{PROJECT}/datasets{path}")

    return send


def test_viewer_reads(call):
    assert call(access()).status_code == 200


def test_viewer_cannot_write_403_names_the_action(call):
    r = call(access(), "post", "?name=x")
    assert r.status_code == 403
    assert (r.json()["code"], r.json()["action"]) == ("forbidden", "dataset.write")


def test_dataset_editor_passes_the_write_check(call):
    # authz lets the request through; the faked row then has no dataset, so the handler answers 404.
    r = call(access(visibility="private", roles=["dataset_editor"]), "post", f"/{DATASET}/label?key=_unlabeled/a&label=b")
    assert r.status_code == 404 and r.json()["detail"] == "not found"


@pytest.mark.parametrize("row", [access(visibility="private"), access(visibility=None, default_access=None, roles=None)])
def test_no_access_and_cross_tenant_are_the_same_404(call, row):
    for method, path in (("get", ""), ("post", "?name=x"), ("get", f"/{DATASET}/labeling")):
        r = call(row, method, path)
        assert (r.status_code, r.json()["code"]) == (404, "not_found")


SUB = [("get", "/{d}/labeling"), ("get", "/{d}/labeling/export"), ("post", "/{d}/labeling"),
       ("post", "/{d}/labeling/import-label-studio-export"), ("get", "/{d}/preprocess"), ("post", "/{d}/preprocess")]


@pytest.mark.parametrize(("method", "path"), SUB)
def test_labeling_and_preprocessing_roles(call, method, path):
    path = path.format(d=DATASET)
    write = method == "post"
    viewer, editor = access(), access(visibility="private", roles=["dataset_editor"])
    r = call(viewer, method, path)
    if write:
        assert (r.status_code, r.json()["action"]) == (403, "dataset.write")
    else:
        assert r.status_code != 403
    assert call(editor, method, path).status_code != 403
    assert call(access(visibility="private"), method, path).status_code == 404
