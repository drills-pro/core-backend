"""/v1/admin/models: listing, version history and totals over the SECURITY DEFINER functions (faked at the connection)."""
import datetime
import uuid

import pytest

from tests.modules.admin.conftest import AdminConn

URL = "/v1/admin/models"
NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
VERSION = uuid.uuid4()
TENANT = uuid.uuid4()


def version(**kw):
    return dict(id=uuid.uuid4(), version="3", stage="staging", status="deployed", created_at=NOW, **kw)


def model(total=7):
    return version(tenant_id=TENANT, tenant_name="Acme", project_id=uuid.uuid4(), project_name="P", task_type="image-classification",
                   visibility="internal", model_name="m", version_count=3, total=total)


def test_listing_shapes_rows_and_reports_the_filtered_total(admin):
    res = admin(AdminConn(fetches={"admin_models(": [model()]})).get(URL)
    assert res.status_code == 200
    assert res.headers["X-Total-Count"] == "7"
    row = res.json()[0]
    assert (row["model_name"], row["version_count"], row["status"], row["tenant_name"]) == ("m", 3, "deployed", "Acme")
    assert "total" not in row  # the pager column is not part of a row


def test_listing_passes_every_filter_to_the_function(admin):
    conn = AdminConn()
    res = admin(conn).get(URL, params={"q": "m", "tenant_id": str(TENANT), "task_type": "image-classification",
                                       "visibility": "private", "status": "stopped", "limit": 5, "offset": 10})
    assert res.status_code == 200
    assert conn.calls[0][1] == (5, 10, TENANT, "image-classification", "private", "stopped", "m")


def test_an_empty_listing_says_zero(admin):
    res = admin(AdminConn()).get(URL)
    assert (res.json(), res.headers["X-Total-Count"]) == ([], "0")


@pytest.mark.parametrize("param, value", [("status", "paused"), ("visibility", "public")])
def test_an_unknown_filter_value_is_a_400_not_an_empty_page(admin, param, value):
    conn = AdminConn()
    res = admin(conn).get(URL, params={param: value})
    assert (res.status_code, res.json()["code"]) == (400, "invalid_filter")
    assert param in res.json()["detail"] and conn.calls == []


def test_an_unknown_task_type_is_422(admin):
    assert admin(AdminConn()).get(URL, params={"task_type": "juggling"}).status_code == 422


def test_versions_are_addressed_by_one_of_them(admin):
    conn = AdminConn(fetches={"admin_model_versions": [dict(version(), total=2), dict(version(), total=2)]})
    res = admin(conn).get(f"{URL}/{VERSION}/versions", params={"limit": 2, "offset": 1})
    assert (res.status_code, len(res.json()), res.headers["X-Total-Count"]) == (200, 2, "2")
    assert conn.calls[0][1] == (VERSION, 2, 1)


def test_versions_of_an_unknown_id_is_an_empty_list(admin):
    res = admin(AdminConn()).get(f"{URL}/{VERSION}/versions")
    assert (res.status_code, res.json(), res.headers["X-Total-Count"]) == (200, [], "0")


def test_summary_is_the_stats_row(admin):
    stats = dict(model_count=5, deployed_count=2, deploying_count=1, stopped_count=1, not_deployed_count=1, project_count=4, version_count=9)
    res = admin(AdminConn(rows={"admin_model_stats": stats})).get(f"{URL}/summary")
    assert (res.status_code, res.json()) == (200, stats)
