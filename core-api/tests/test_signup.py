"""Saga ordering and compensation, with Zitadel and Postgres faked out."""
import asyncio

import httpx
import pytest
from fastapi import HTTPException

from app.integrations import zitadel
from app.modules.user.auth import signup


class FakeConn:
    def __init__(self, db):
        self.db = db

    async def execute(self, sql, *args):
        self.db.setdefault("sql", []).append(sql)

    async def fetchrow(self, sql, *args):
        return self.db["row"]

    async def fetch(self, sql, *args):
        return []


class FakePool:
    def __init__(self, db):
        self.db = db

    def acquire(self):
        db = self.db

        class Ctx:
            async def __aenter__(self):
                return FakeConn(db)

            async def __aexit__(self, *exc):
                return False

        return Ctx()


@pytest.fixture
def env(monkeypatch):
    db = {"row": {"email_hash": signup._email_hash("a@b.co"), "state": "started", "org_id": None, "user_id": None, "tenant_id": None}}
    calls = []

    async def get_pool():
        return FakePool(db)

    async def tenant(name, org_id):
        calls.append("tenant")
        return "11111111-1111-1111-1111-111111111111"

    async def org(*a):
        calls.append("org")
        return "org-1", "user-1"

    async def pgrant(*a):
        calls.append("pgrant")
        return "grant-1"

    async def ugrant(*a):
        calls.append("ugrant")

    async def delete(client, org_id):
        calls.append(f"delete:{org_id}")

    monkeypatch.setattr(signup, "get_pool", get_pool)
    monkeypatch.setattr(signup, "_create_tenant", tenant)
    monkeypatch.setattr(signup.auth, "project_id", lambda: "proj")
    async def allow(*a):
        return True

    monkeypatch.setattr(signup.rate_limit, "check", allow)
    monkeypatch.setattr(zitadel, "create_org_with_owner", org)
    monkeypatch.setattr(zitadel, "create_project_grant", pgrant)
    monkeypatch.setattr(zitadel, "create_user_grant", ugrant)
    monkeypatch.setattr(zitadel, "delete_org", delete)
    return db, calls


def _req():
    class R:
        headers = {}
        client = None

    return R()


def _body():
    return signup.SignupRequest(org_name="Acme", first_name="A", last_name="B", email="a@b.co", password="pw")


def test_happy_path_runs_zitadel_before_postgres(env):
    _, calls = env
    out = asyncio.run(signup.signup(_body(), _req(), idempotency_key="k"))
    assert calls == ["org", "pgrant", "ugrant", "tenant"]
    assert out["org_id"] == "org-1"


def test_failure_deletes_org_and_reports_zitadel_message(env, monkeypatch):
    _, calls = env

    async def boom(*a):
        raise httpx.HTTPStatusError(
            "x", request=httpx.Request("POST", "http://z"), response=httpx.Response(409, json={"message": "taken"})
        )

    monkeypatch.setattr(zitadel, "create_project_grant", boom)
    with pytest.raises(HTTPException) as e:
        asyncio.run(signup.signup(_body(), _req(), idempotency_key="k"))
    assert e.value.status_code == 409 and e.value.detail == "taken"
    assert "delete:org-1" in calls and "tenant" not in calls


def test_resume_from_org_state_skips_org_creation(env):
    db, calls = env
    db["row"] = {**db["row"], "state": "org", "org_id": "org-9", "user_id": "u-9"}
    asyncio.run(signup.signup(_body(), _req(), idempotency_key="k"))
    assert calls == ["pgrant", "ugrant", "tenant"]


def test_key_reuse_for_other_email_is_rejected(env):
    db, _ = env
    db["row"] = {**db["row"], "email_hash": signup._email_hash("other@b.co")}
    with pytest.raises(HTTPException) as e:
        asyncio.run(signup.signup(_body(), _req(), idempotency_key="k"))
    assert e.value.status_code == 409
