"""A signed-in operator's own password account (app/modules/admin/auth/
account.py). The database is a fake holding one admin_accounts row."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import asyncpg
import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import auth, config
from app.modules.admin.auth import account, password

SECRET = "test-local-token-secret-0123456789abcdef"
EPOCH = datetime(2020, 1, 1, tzinfo=timezone.utc)


def _row(**overrides):
    return {
        "id": "a1",
        "username": "alice",
        "display_name": "Alice",
        "email": "alice@example.com",
        "phone": None,
        "theme": "system",
        "table_density": "comfortable",
        "language": "en",
        "time_zone": "Asia/Bangkok",
        "date_format": "dd/mm/yyyy",
        "notify_new_organization": True,
        "notify_deployment_failed": True,
        "notify_suspension": True,
        "weekly_digest": False,
        "confirm_destructive": True,
        "created_at": EPOCH,
        "last_sign_in_at": None,
        "password_changed_at": EPOCH,
        "disabled": False,
        "password_hash": password.hash_password("current-password"),
        **overrides,
    }


class FakeConn:
    """One row; an UPDATE ... SET col = $n applies to it, so a read after a
    write sees the write."""

    def __init__(self, row):
        self.row = row
        self.updates: list[tuple] = []
        self.raise_on_update: Exception | None = None

    async def fetchrow(self, sql, *args):
        return self.row

    async def fetchval(self, sql, *args):
        # pg_timezone_names, cut down to what the tests ask about.
        return args[0] in {"UTC", "Asia/Bangkok", "Europe/London"}

    async def execute(self, sql, *args):
        self.updates.append((sql, args))
        if self.raise_on_update:
            raise self.raise_on_update
        if sql.startswith("UPDATE admin_accounts SET") and "WHERE lower(username)" in sql:
            assignments = sql.split(" SET ", 1)[1].split(", updated_at", 1)[0]
            for part in assignments.split(", "):
                column, placeholder = part.split(" = $")
                self.row[column] = args[int(placeholder) - 1]
        return "UPDATE 1"


@pytest.fixture(autouse=True)
def secret(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_LOCAL_TOKEN_SECRET", SECRET)
    monkeypatch.setattr(password, "SCRYPT_N", 2**4)


@pytest.fixture
def conn(monkeypatch):
    conn = FakeConn(_row())

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield conn

    async def get_pool():
        return Pool()

    monkeypatch.setattr(account, "get_pool", get_pool)
    monkeypatch.setattr(auth.db, "get_pool", get_pool)
    return conn


@pytest.fixture
def client(conn):
    app = FastAPI()
    app.include_router(account.router)
    return TestClient(app)


def _headers(token=None):
    return {"Authorization": f"Bearer {token or auth.issue_local_token('alice')}"}


# --- who may call it -----------------------------------------------------------


def test_reads_the_callers_own_account(client):
    resp = client.get("/v1/admin/account", headers=_headers())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["username"] == "alice"
    assert body["display_name"] == "Alice"
    assert body["settings"]["theme"] == "system"
    assert "password_hash" not in body and "disabled" not in body


def test_needs_a_token(client):
    assert client.get("/v1/admin/account").status_code == 401
    # The dev header vouches for nobody in particular.
    assert client.get("/v1/admin/account", headers={"X-Admin-User": "local:alice"}).status_code == 401


def test_turns_away_a_zitadel_token(client):
    zitadel_like = jwt.encode({"iss": "https://zitadel.example", "sub": "123"}, "k" * 32, algorithm="HS256")
    resp = client.get("/v1/admin/account", headers=_headers(zitadel_like))
    assert resp.status_code == 403


def test_turns_away_a_disabled_account(client, conn):
    conn.row["disabled"] = True
    assert client.get("/v1/admin/account", headers=_headers()).status_code == 401


# --- profile ------------------------------------------------------------------


def test_updates_only_the_profile_fields_sent(client, conn):
    resp = client.patch("/v1/admin/account/profile", headers=_headers(), json={"phone": " +66 81 234 5678 "})
    assert resp.status_code == 200, resp.text
    assert resp.json()["phone"] == "+66 81 234 5678"
    assert resp.json()["display_name"] == "Alice"
    ((sql, args),) = [u for u in conn.updates if "phone" in u[0]]
    assert "display_name" not in sql and "email" not in sql


def test_an_empty_email_or_phone_clears_it(client, conn):
    conn.row["phone"] = "123"
    resp = client.patch("/v1/admin/account/profile", headers=_headers(), json={"email": "", "phone": " "})
    assert resp.status_code == 200, resp.text
    assert resp.json()["email"] is None and resp.json()["phone"] is None


def test_the_display_name_cannot_be_emptied(client):
    resp = client.patch("/v1/admin/account/profile", headers=_headers(), json={"display_name": "  "})
    assert resp.status_code == 422


def test_rejects_something_that_is_not_an_email(client):
    resp = client.patch("/v1/admin/account/profile", headers=_headers(), json={"email": "alice"})
    assert resp.status_code == 422


def test_an_email_another_account_has_is_a_conflict(client, conn):
    conn.raise_on_update = asyncpg.UniqueViolationError("dup")
    resp = client.patch("/v1/admin/account/profile", headers=_headers(), json={"email": "bob@example.com"})
    assert resp.status_code == 409


# --- settings -----------------------------------------------------------------


def test_updates_settings_and_returns_all_of_them(client):
    resp = client.patch(
        "/v1/admin/account/settings",
        headers=_headers(),
        json={"theme": "dark", "weekly_digest": True, "time_zone": "UTC"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["theme"] == "dark" and body["weekly_digest"] is True and body["time_zone"] == "UTC"
    assert body["language"] == "en"


@pytest.mark.parametrize(
    "change",
    [{"theme": "purple"}, {"language": "fr"}, {"date_format": "yyyy"}, {"time_zone": "Mars/Olympus"}],
)
def test_rejects_a_setting_outside_its_choices(client, change):
    assert client.patch("/v1/admin/account/settings", headers=_headers(), json=change).status_code == 422


def test_a_null_setting_changes_nothing(client, conn):
    resp = client.patch("/v1/admin/account/settings", headers=_headers(), json={"theme": None})
    assert resp.status_code == 200
    assert not [u for u in conn.updates if u[0].startswith("UPDATE admin_accounts SET")]


# --- password -----------------------------------------------------------------


def _change(client, current="current-password", new="a-brand-new-password"):
    return client.post(
        "/v1/admin/account/password",
        headers=_headers(),
        json={"current_password": current, "new_password": new},
    )


def test_changes_the_password_and_hands_back_a_token_that_still_works(client, conn):
    resp = _change(client)
    assert resp.status_code == 200, resp.text
    ((sql, args),) = [u for u in conn.updates if "password_hash" in u[0]]
    new_hash, changed_at = args[1], args[2]
    assert password.verify_password("a-brand-new-password", new_hash)
    assert changed_at.microsecond == 0

    # The fresh token passes the check that turns away anything older
    # than the change.
    conn.row["password_changed_at"] = changed_at
    token = resp.json()["access_token"]
    assert client.get("/v1/admin/account", headers=_headers(token)).status_code == 200


def test_a_wrong_current_password_counts_towards_the_lockout(client, conn):
    resp = _change(client, current="wrong")
    assert resp.status_code == 403
    ((sql, args),) = conn.updates
    assert "failed_attempts" in sql and args[1:] == (password.MAX_FAILED_ATTEMPTS, password.LOCKOUT)


def test_a_short_new_password_is_refused_before_anything_is_checked(client, conn):
    assert _change(client, new="short").status_code == 422
    assert conn.updates == []


def test_the_new_password_must_differ(client):
    assert _change(client, new="current-password").status_code == 422
