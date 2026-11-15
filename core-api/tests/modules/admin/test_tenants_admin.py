"""/v1/admin/tenants through the real routers: Postgres faked at the connection, Zitadel at the HTTP transport."""
import datetime
import json
import uuid

import asyncpg
import httpx
import pytest

from tests.modules.admin.conftest import AdminConn, zitadel_orgs

T1, T2, T3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
URL = "/v1/admin/tenants"
ORG_SEARCH, MEMBER_SEARCH, USER_SEARCH = "/admin/v1/orgs/_search", "/members/_search", "/users/_search"
LIST_SQL = "admin_tenant_stats"


def tenant_row(tid, name, org, **kw):
    return dict(id=tid, name=name, zitadel_org_id=org, plan_tier="free", created_at=NOW, project_count=2, member_count=3, **kw)


def owners(*members):
    return (200, {"result": list(members)})


HUMAN = {"userId": "u-human", "userType": "TYPE_HUMAN", "roles": ["ORG_OWNER"], "displayName": "Ada", "preferredLoginName": "ada@x"}
MACHINE = {"userId": "u-bot", "userType": "TYPE_MACHINE", "roles": ["ORG_OWNER"], "preferredLoginName": "bot"}


@pytest.fixture
def three_tenants(zitadel_stub):
    zitadel_stub.routes[ORG_SEARCH] = zitadel_orgs(o1="ORG_STATE_ACTIVE", o2="ORG_STATE_INACTIVE")
    zitadel_stub.routes[MEMBER_SEARCH] = owners(MACHINE, HUMAN)
    return AdminConn(fetches={LIST_SQL: [tenant_row(T1, "A", "o1"), tenant_row(T2, "B", "o2"), tenant_row(T3, "C", None)]})


def test_listing_joins_zitadel_status_and_owner(admin, three_tenants):
    res = admin(three_tenants).get(URL)
    assert res.status_code == 200
    assert [(r["name"], r["status"]) for r in res.json()] == [("A", "active"), ("B", "suspended"), ("C", "unknown")]
    assert res.json()[0]["owner"] == {"user_id": "u-human", "display_name": "Ada", "login_name": "ada@x"}  # human beats the machine user
    assert res.json()[2]["owner"] is None  # no org, nobody to ask
    assert res.headers["X-Total-Count"] == "3"
    assert json.loads(res.headers["X-Status-Counts"]) == {"active": 1, "suspended": 1, "unknown": 1}


def test_status_filter_narrows_the_page_but_not_the_counts(admin, three_tenants):
    res = admin(three_tenants).get(URL, params={"status": "suspended"})
    assert [r["name"] for r in res.json()] == ["B"]
    assert res.headers["X-Total-Count"] == "1"
    assert json.loads(res.headers["X-Status-Counts"])["active"] == 1


def test_listing_pages_the_filtered_set(admin, three_tenants):
    res = admin(three_tenants).get(URL, params={"limit": 1, "offset": 1})
    assert [r["name"] for r in res.json()] == ["B"]
    assert res.headers["X-Total-Count"] == "3"


def test_listing_passes_the_filters_to_the_query(admin, three_tenants):
    admin(three_tenants).get(URL, params={"q": "ac", "plan_tier": "pro"})
    sql, args = three_tenants.calls[0]
    assert args[:2] == ("ac", "pro")


def test_listing_survives_zitadel_being_down(admin, zitadel_stub):
    zitadel_stub.routes[ORG_SEARCH] = httpx.ConnectError("down")
    zitadel_stub.routes[MEMBER_SEARCH] = httpx.ConnectError("down")
    conn = AdminConn(fetches={LIST_SQL: [tenant_row(T1, "A", "o1")]})
    res = admin(conn).get(URL)
    assert res.status_code == 200
    assert (res.json()[0]["status"], res.json()[0]["owner"]) == ("unknown", None)


def test_summary_counts_in_postgres_and_statuses_from_the_tenants_own_orgs(admin, zitadel_stub):
    # org-x lives in Zitadel with no tenant row: it must not be counted
    zitadel_stub.routes[ORG_SEARCH] = zitadel_orgs(o1="ORG_STATE_ACTIVE", o2="ORG_STATE_INACTIVE", ox="ORG_STATE_ACTIVE")
    conn = AdminConn(
        rows={"admin_platform_stats": dict(tenant_count=4, tenant_count_30d=1, project_count=9, project_count_30d=2, member_count=7)},
        fetches={"zitadel_org_id FROM tenants": [{"zitadel_org_id": o} for o in ("o1", "o2", None)]},
    )
    res = admin(conn).get(f"{URL}/summary")
    assert res.status_code == 200
    assert res.json() == {
        "organizations": 4, "organizations_new_30d": 1, "projects": 9, "projects_new_30d": 2, "members": 7,
        "status_counts": {"active": 1, "suspended": 1, "unknown": 1},
    }


# --- create ---------------------------------------------------------------------------------------------------

TEAM = {"id": uuid.uuid4(), "name": "Default", "created_at": NOW}


def created(plan="free", org="org-new"):
    return {"id": T1, "name": "Acme", "zitadel_org_id": org, "plan_tier": plan, "created_at": NOW}


def test_create_makes_the_org_the_tenant_and_the_default_team(admin, zitadel_stub):
    zitadel_stub.routes["/management/v1/orgs"] = (200, {"id": "org-new"})
    conn = AdminConn(rows={"INSERT INTO tenants": created(), "INSERT INTO teams": TEAM})
    res = admin(conn).post(URL, json={"name": "  Acme  "})
    assert res.status_code == 201
    assert (res.json()["zitadel_org_id"], res.json()["status"]) == ("org-new", "active")
    assert json.loads(zitadel_stub.requests[0].content) == {"name": "Acme"}  # trimmed
    inserts = [args for sql, args in conn.calls if sql.startswith("INSERT INTO tenants")]
    assert inserts == [("org-new", "Acme", "free")]
    assert any(sql.startswith("INSERT INTO teams") for sql, _ in conn.calls)


def test_create_adopting_an_org_does_not_call_zitadel_and_reports_unknown(admin, zitadel_stub):
    conn = AdminConn(rows={"INSERT INTO tenants": created(org="existing"), "INSERT INTO teams": TEAM})
    res = admin(conn).post(URL, json={"name": "Acme", "zitadel_org_id": "existing"})
    assert (res.status_code, res.json()["status"]) == (201, "unknown")
    assert zitadel_stub.requests == []


@pytest.mark.parametrize("body, code", [({"name": "   "}, "name_required"), ({"name": "x", "plan_tier": "gold"}, "invalid_plan_tier")])
def test_create_validates_before_touching_anything(admin, zitadel_stub, body, code):
    conn = AdminConn()
    res = admin(conn).post(URL, json=body)
    assert (res.status_code, res.json()["code"]) == (400, code)
    assert zitadel_stub.requests == [] and conn.calls == []


@pytest.mark.parametrize("answer, status, code", [
    ((409, {"message": "Organisation already exists"}), 409, "zitadel_refused"),
    ((500, {"message": "boom"}), 502, "zitadel_refused"),
    (httpx.ConnectError("down"), 502, "zitadel_unreachable"),
])
def test_create_maps_zitadel_failures(admin, zitadel_stub, answer, status, code):
    zitadel_stub.routes["/management/v1/orgs"] = answer
    conn = AdminConn()
    res = admin(conn).post(URL, json={"name": "Acme"})
    assert (res.status_code, res.json()["code"]) == (status, code)
    if isinstance(answer, tuple):
        assert res.json()["detail"] == answer[1]["message"]  # Zitadel's own wording
    assert conn.calls == []  # no tenant row for an org that was never made


def test_create_refusal_with_a_non_json_body_passes_the_text_through(admin, zitadel_stub):
    zitadel_stub.routes["/management/v1/orgs"] = (502, "<html>bad gateway</html>")
    res = admin(AdminConn()).post(URL, json={"name": "Acme"})
    assert (res.status_code, res.json()["code"], res.json()["detail"]) == (502, "zitadel_refused", "<html>bad gateway</html>")


def test_create_deletes_the_org_it_made_when_the_row_collides(admin, zitadel_stub):
    zitadel_stub.routes["/management/v1/orgs"] = (200, {"id": "org-new"})
    zitadel_stub.routes["/admin/v1/orgs/org-new"] = (200, {})
    conn = AdminConn(rows={"INSERT INTO tenants": asyncpg.UniqueViolationError()})
    res = admin(conn).post(URL, json={"name": "Acme"})
    assert (res.status_code, res.json()["code"]) == (409, "org_already_mapped")
    assert [r.method for r in zitadel_stub.seen("/admin/v1/orgs/org-new")] == ["DELETE"]


def test_create_leaves_an_adopted_org_alone_when_the_row_collides(admin, zitadel_stub):
    conn = AdminConn(rows={"INSERT INTO tenants": asyncpg.UniqueViolationError()})
    res = admin(conn).post(URL, json={"name": "Acme", "zitadel_org_id": "mine"})
    assert (res.status_code, res.json()["code"]) == (409, "org_already_mapped")
    assert zitadel_stub.requests == []


def test_a_failed_cleanup_does_not_replace_the_original_error(admin, zitadel_stub):
    zitadel_stub.routes["/management/v1/orgs"] = (200, {"id": "org-new"})
    zitadel_stub.routes["/admin/v1/orgs/org-new"] = (500, {})
    conn = AdminConn(rows={"INSERT INTO tenants": asyncpg.UniqueViolationError()})
    res = admin(conn).post(URL, json={"name": "Acme"})
    assert (res.status_code, res.json()["code"]) == (409, "org_already_mapped")


# --- detail and sub-listings ------------------------------------------------------------------------------------

DETAIL = dict(tenant_row(T1, "A", "o1"), settings={"allow_public_models": True}, dataset_count=4, model_count=5)


def test_detail_assembles_row_status_and_owner(admin, zitadel_stub):
    zitadel_stub.routes[ORG_SEARCH] = zitadel_orgs(o1="ORG_STATE_INACTIVE")
    zitadel_stub.routes[MEMBER_SEARCH] = owners(HUMAN)
    res = admin(AdminConn(rows={"admin_tenant_detail_stats": DETAIL})).get(f"{URL}/{T1}")
    body = res.json()
    assert res.status_code == 200
    assert (body["status"], body["owner"]["login_name"], body["dataset_count"], body["model_count"]) == ("suspended", "ada@x", 4, 5)
    assert body["settings"]["allow_public_models"] is True


def test_detail_of_an_unknown_tenant_is_404(admin, zitadel_stub):
    res = admin(AdminConn(rows={"admin_tenant_detail_stats": None})).get(f"{URL}/{T1}")
    assert (res.status_code, res.json()["code"]) == (404, "not_found")
    assert zitadel_stub.requests == []


def test_projects_and_datasets_are_the_sql_function_rows(admin):
    project = dict(id=uuid.uuid4(), name="P", status="active", task_type=None, visibility="internal", created_at=NOW,
                   dataset_count=1, model_count=0, model_name=None, model_version=None)
    dataset = dict(id=uuid.uuid4(), name="D", project_id=project["id"], project_name="P", import_status=None, created_at=NOW)
    conn = AdminConn(fetches={"admin_tenant_projects": [project], "admin_tenant_datasets": [dataset]})
    client = admin(conn)
    assert [p["name"] for p in client.get(f"{URL}/{T1}/projects").json()] == ["P"]
    assert [d["name"] for d in client.get(f"{URL}/{T1}/datasets").json()] == ["D"]
    assert [args for _, args in conn.calls] == [(T1,), (T1,)]


def member_row(sub, **kw):
    return dict(user_id=sub, roles=["project_admin"], projects=["P"], project_count=1, joined_at=NOW, **kw)


def test_members_are_joined_with_zitadel_and_keep_unknown_ones(admin, zitadel_stub):
    zitadel_stub.routes[USER_SEARCH] = (200, {"result": [
        {"id": "u1", "preferredLoginName": "ann@x", "state": "USER_STATE_INITIAL",
         "human": {"profile": {"displayName": "Ann"}, "email": {"email": "ann@mail"}}},
        {"id": "bot", "preferredLoginName": "bot", "state": "USER_STATE_LOCKED"},
    ]})
    conn = AdminConn(fetches={"FROM tenants WHERE id = ANY": [{"id": T1, "zitadel_org_id": "o1"}],
                              "admin_tenant_members": [member_row("u1"), member_row("gone")]})
    res = admin(conn).get(f"{URL}/{T1}/members")
    assert res.status_code == 200
    ann, gone = res.json()
    assert (ann["display_name"], ann["email"], ann["status"]) == ("Ann", "ann@mail", "pending")
    assert (gone["user_id"], gone["display_name"], gone["status"]) == ("gone", None, "unknown")
    assert zitadel_stub.requests[0].headers["x-zitadel-orgid"] == "o1"


def test_members_of_a_tenant_without_an_org_skip_zitadel(admin, zitadel_stub):
    conn = AdminConn(fetches={"FROM tenants WHERE id = ANY": [{"id": T1, "zitadel_org_id": None}],
                              "admin_tenant_members": [member_row("u1")]})
    res = admin(conn).get(f"{URL}/{T1}/members")
    assert res.json()[0]["status"] == "unknown"
    assert zitadel_stub.requests == []


def test_members_of_an_unknown_tenant_is_404(admin):
    res = admin(AdminConn(fetches={"FROM tenants WHERE id = ANY": []})).get(f"{URL}/{T1}/members")
    assert (res.status_code, res.json()["code"]) == (404, "not_found")


# --- create project ---------------------------------------------------------------------------------------------

PROJECT = dict(id=uuid.uuid4(), name="P", status="active", team_id=TEAM["id"], task_type="image-classification",
               visibility="private", default_access="read", gallery_published=False, created_at=NOW)
BODY = {"name": "P", "task_type": "image-classification"}


def test_create_project_lands_in_the_default_team_and_is_audited(admin):
    conn = AdminConn(rows={"FROM tenants WHERE id": {"id": T1, "name": "A", "slug": "a"}, "INSERT INTO projects": PROJECT},
                     vals={"FROM teams": TEAM["id"]})
    res = admin(conn).post(f"{URL}/{T1}/projects", json=BODY)
    assert res.status_code == 201
    assert res.json()["name"] == "P" and "team_id" not in res.json()
    insert = next(args for sql, args in conn.calls if sql.startswith("INSERT INTO projects"))
    assert insert == (T1, "P", TEAM["id"], "image-classification")
    assert any("audit_log" in sql for sql, _ in conn.calls)


def test_create_project_in_an_unknown_tenant_is_404(admin):
    res = admin(AdminConn(rows={"FROM tenants WHERE id": None})).post(f"{URL}/{T1}/projects", json=BODY)
    assert (res.status_code, res.json()["code"]) == (404, "not_found")


def test_create_project_in_a_tenant_without_a_team_is_409(admin):
    conn = AdminConn(rows={"FROM tenants WHERE id": {"id": T1, "name": "A", "slug": "a"}}, vals={"FROM teams": None})
    res = admin(conn).post(f"{URL}/{T1}/projects", json=BODY)
    assert (res.status_code, res.json()["code"]) == (409, "no_team")
    assert not any(sql.startswith("INSERT INTO projects") for sql, _ in conn.calls)


# --- settings and status ----------------------------------------------------------------------------------------

def test_settings_patch_merges_only_the_fields_sent(admin):
    conn = AdminConn(rows={"UPDATE tenants SET settings": {"settings": {"allow_public_models": True}}})
    res = admin(conn).patch(f"{URL}/{T1}/settings", json={"allow_public_models": True})
    assert res.status_code == 200
    assert res.json()["settings"] == {"members_can_create_projects": True, "require_review_before_training": True, "allow_public_models": True}
    assert conn.calls[0][1] == (T1, {"allow_public_models": True})


def test_settings_patch_of_an_unknown_tenant_is_404(admin):
    res = admin(AdminConn(rows={"UPDATE tenants SET settings": None})).patch(f"{URL}/{T1}/settings", json={"allow_public_models": True})
    assert res.status_code == 404


def test_settings_patch_rejects_unknown_and_non_boolean_values(admin):
    client = admin(AdminConn())
    assert client.patch(f"{URL}/{T1}/settings", json={"surprise": True}).status_code == 422
    assert client.patch(f"{URL}/{T1}/settings", json={"allow_public_models": "yes"}).status_code == 422


def test_status_pending_delete_is_not_an_operator_switch(admin):
    conn = AdminConn()
    res = admin(conn).post(f"{URL}/{T1}/status", json={"status": "pending_delete"})
    assert (res.status_code, res.json()["code"]) == (400, "status_not_settable")
    assert conn.calls == []


def test_status_of_an_unknown_tenant_is_404(admin):
    res = admin(AdminConn(rows={"UPDATE tenants": None})).post(f"{URL}/{T1}/status", json={"status": "suspended"})
    assert res.status_code == 404


@pytest.mark.parametrize("answer, status, code", [
    ((409, {"message": "Organisation is already deactivated"}), 409, "zitadel_refused"),
    (httpx.ConnectError("down"), 502, "zitadel_unreachable"),
])
def test_status_surfaces_zitadel_failures(admin, zitadel_stub, answer, status, code):
    zitadel_stub.routes["/_deactivate"] = answer
    conn = AdminConn(rows={"UPDATE tenants": {"previous": "active", "org_id": "o1"}})
    res = admin(conn).post(f"{URL}/{T1}/status", json={"status": "suspended"})
    assert (res.status_code, res.json()["code"]) == (status, code)
    assert zitadel_stub.requests[0].headers["x-zitadel-orgid"] == "o1"
