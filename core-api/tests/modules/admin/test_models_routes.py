"""/v1/admin/models and /v1/admin/tenants/{id}/datasets through the real routers, database faked."""
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import problem
from app.main import create_app
from app.modules.admin.models import routes as model_routes, service as model_service
from app.modules.admin.models.constants import TOTAL_COLUMN, ModelStatus, ProblemCode
from app.modules.admin.tenants import detail, service as tenant_service
from tests.support import FakeConn, TenantCtx

NOW = datetime(2026, 11, 10, tzinfo=UTC)
ID = uuid.uuid4()
TOTAL = 7


def version(**kw):
    return {"id": ID, "version": "3", "stage": "Production", "status": ModelStatus.DEPLOYED, "created_at": NOW, TOTAL_COLUMN: TOTAL, **kw}


def model(**kw):
    return version(tenant_id=uuid.uuid4(), tenant_name="Acme", project_id=uuid.uuid4(), project_name="signs", task_type="image_classification",
                   visibility="private", model_name="model", version_count=3, **kw)


@pytest.fixture
def call(monkeypatch):
    def run(module, router, conn, path):
        async def get_pool():
            return SimpleNamespace(acquire=lambda: TenantCtx(conn))

        monkeypatch.setattr(module, "get_pool", get_pool)
        app = FastAPI()
        problem.install(app)
        app.dependency_overrides[model_routes.get_platform_admin] = lambda: "ops"
        app.dependency_overrides[detail.get_platform_admin] = lambda: "ops"
        app.include_router(router)
        return TestClient(app).get(path)

    return run


def test_the_list_is_one_row_per_model_with_the_filtered_total_in_a_header(call):
    conn = FakeConn(rowset=[model()])
    res = call(model_service, model_routes.router, conn, "/v1/admin/models?status=deployed&limit=10&offset=20")
    assert res.status_code == 200 and res.headers["X-Total-Count"] == str(TOTAL)
    assert res.json()[0]["model_name"] == "model" and res.json()[0]["version_count"] == 3
    args = conn.calls[0][1]
    assert args[:2] == (10, 20) and args[5] == ModelStatus.DEPLOYED  # limit, offset, tenant, task, visibility, status, q


def test_an_empty_page_still_says_zero_in_the_total_header(call):
    res = call(model_service, model_routes.router, FakeConn(rowset=[]), "/v1/admin/models")
    assert res.json() == [] and res.headers["X-Total-Count"] == "0"


@pytest.mark.parametrize("filter", ["status=nope", "visibility=nope"])
def test_an_unknown_filter_is_a_400_not_an_empty_page(call, filter):
    conn = FakeConn()
    res = call(model_service, model_routes.router, conn, f"/v1/admin/models?{filter}")
    assert res.status_code == 400 and res.json()["code"] == ProblemCode.INVALID_FILTER and conn.calls == []


def test_versions_are_listed_for_the_version_id_the_listing_handed_out(call):
    conn = FakeConn(rowset=[version(), version(version="2", stage="None")])
    res = call(model_service, model_routes.router, conn, f"/v1/admin/models/{ID}/versions")
    assert [v["version"] for v in res.json()] == ["3", "2"] and res.headers["X-Total-Count"] == str(TOTAL)
    assert conn.calls[0][1][0] == ID


def test_an_id_that_names_no_version_is_an_empty_list(call):
    res = call(model_service, model_routes.router, FakeConn(rowset=[]), f"/v1/admin/models/{ID}/versions")
    assert res.status_code == 200 and res.json() == []


def test_the_summary_is_the_platform_wide_counts(call):
    stats = dict(model_count=5, deployed_count=2, deploying_count=1, stopped_count=1, not_deployed_count=1, project_count=3, version_count=9)
    res = call(model_service, model_routes.router, FakeConn(row=stats), "/v1/admin/models/summary")
    assert res.json() == stats


def test_the_admin_routes_refuse_a_caller_with_no_valid_token():
    client = TestClient(create_app())
    for path in ("/v1/admin/models", "/v1/admin/models/summary", f"/v1/admin/models/{ID}/versions", f"/v1/admin/tenants/{ID}/datasets"):
        assert client.get(path, headers={"Authorization": "Bearer not-a-token"}).status_code == 401


def test_a_tenants_datasets_are_listed_with_their_project_and_import_state(call):
    row = {"id": ID, "name": "signs", "project_id": uuid.uuid4(), "project_name": "p", "import_status": None, "created_at": NOW}
    conn = FakeConn(rowset=[row])
    res = call(tenant_service, detail.router, conn, f"/v1/admin/tenants/{ID}/datasets")
    assert res.status_code == 200 and res.json()[0]["name"] == "signs" and res.json()[0]["import_status"] is None
    assert conn.calls[0][1] == (ID,)
