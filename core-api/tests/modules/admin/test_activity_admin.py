"""/v1/admin/activity: the audit rows from Postgres, the actor's name from each tenant's Zitadel org."""
import datetime
import uuid

import httpx

from tests.modules.admin.conftest import AdminConn

T1, T2 = uuid.uuid4(), uuid.uuid4()
NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
URL = "/v1/admin/activity"
USER_SEARCH = "/management/v1/users/_search"
USERS = {
    "o1": {"result": [{"id": "sub", "preferredLoginName": "ann@x", "human": {"profile": {"displayName": "Ann"}}}]},
    "o2": {"result": [{"id": "sub", "preferredLoginName": "bob@x"}, {"id": "nameless", "state": "USER_STATE_ACTIVE"}]},
}


def event(tenant, actor, total=3):
    return dict(id=uuid.uuid4(), tenant_id=tenant, tenant_name="T", project_id=None, project_name=None, actor=actor,
                action="create", entity_type="project", entity_id=None, created_at=NOW, total=total)


def conn_with(*events):
    return AdminConn(fetches={
        "admin_activity(": list(events),
        "FROM tenants WHERE id = ANY": [{"id": T1, "zitadel_org_id": "o1"}, {"id": T2, "zitadel_org_id": "o2"}],
    })


def test_rows_get_the_actor_name_from_their_own_tenants_org(admin, zitadel_stub):
    # the same subject is a different account, with a different name, in each org
    zitadel_stub.routes[USER_SEARCH] = lambda request: (200, USERS[request.headers["x-zitadel-orgid"]])
    res = admin(conn_with(event(T1, "sub"), event(T2, "sub"), event(T1, "stranger"))).get(URL)
    assert res.status_code == 200
    assert [r["actor_name"] for r in res.json()["items"]] == ["Ann", "bob@x", None]
    assert res.headers["X-Total-Count"] == "3"


def test_the_log_still_answers_when_zitadel_is_down(admin, zitadel_stub):
    zitadel_stub.routes[USER_SEARCH] = httpx.ConnectError("down")
    res = admin(conn_with(event(T1, "sub"))).get(URL)
    assert res.status_code == 200 and res.json()["items"][0]["actor_name"] is None


def test_an_empty_match_reports_zero_and_never_calls_zitadel(admin, zitadel_stub):
    res = admin(AdminConn(fetches={"admin_activity(": []})).get(URL, params={"q": "nothing"})
    assert (res.json(), res.headers["X-Total-Count"]) == ({"items": [], "next_page_token": None}, "0")
    assert zitadel_stub.requests == []


def test_filters_reach_the_query_and_days_becomes_a_since_cutoff(admin, zitadel_stub):
    conn = conn_with()
    before = datetime.datetime.now(datetime.timezone.utc)
    admin(conn).get(URL, params={"q": "bob", "tenant_id": str(T1), "entity_type": "project", "days": 7, "page_size": 5})
    limit, after_ts, after_id, tenant, entity, q, since = conn.calls[0][1]
    assert (limit, after_ts, after_id, tenant, entity, q) == (6, None, None, T1, "project", "bob")  # one extra row signals the next page
    assert abs((before - since) - datetime.timedelta(days=7)) < datetime.timedelta(seconds=5)


def test_without_days_there_is_no_cutoff(admin):
    conn = conn_with()
    admin(conn).get(URL)
    assert conn.calls[0][1][-1] is None


def test_out_of_range_paging_is_422(admin):
    client = admin(conn_with())
    assert client.get(URL, params={"page_size": 0}).status_code == 422
    assert client.get(URL, params={"days": 0}).status_code == 422


def test_summary_is_the_stats_row(admin):
    conn = AdminConn(rows={"admin_activity_stats": {"events_today": 12, "tenants_today": 3}})
    res = admin(conn).get(f"{URL}/summary")
    assert (res.status_code, res.json()) == (200, {"events_today": 12, "tenants_today": 3})
