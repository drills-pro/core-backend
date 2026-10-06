"""The admin console's own password accounts (app/modules/admin/auth/
password.py) and the tokens core-api issues them — no Zitadel involved.
The database is a fake that answers the few queries these paths make."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.core import auth, config
from app.modules.admin.auth import password, routes

SECRET = "test-local-token-secret-0123456789abcdef"
ALICE = {"id": "a1", "username": "alice", "email": "alice@example.com", "display_name": "Alice"}


class FakeConn:
    """admin_accounts as one row (or none), recording every UPDATE."""

    def __init__(self, row: dict | None):
        self.row = row
        self.updates: list[tuple] = []

    async def fetchrow(self, sql, *args):
        return self.row

    async def execute(self, sql, *args):
        self.updates.append((sql, args))
        return "UPDATE 1"


def _fake_db(monkeypatch, row):
    conn = FakeConn(row)

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield conn

    async def get_pool():
        return Pool()

    monkeypatch.setattr(password, "get_pool", get_pool)
    monkeypatch.setattr(auth.db, "get_pool", get_pool)
    return conn


@pytest.fixture(autouse=True)
def secret(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_LOCAL_TOKEN_SECRET", SECRET)
    # Cheap scrypt for the tests; the stored string records its own cost,
    # so verifying follows whatever the hash says.
    monkeypatch.setattr(password, "SCRYPT_N", 2**4)


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(routes.router)

    @app.get("/whoami")
    async def whoami(actor: str = Depends(auth.get_platform_admin)):
        return {"actor": actor}

    return TestClient(app)


# --- hashing ------------------------------------------------------------------


def test_a_hash_verifies_its_own_password_only():
    stored = password.hash_password("correct horse battery")
    assert stored.startswith("scrypt$")
    assert "correct horse" not in stored
    assert password.verify_password("correct horse battery", stored)
    assert not password.verify_password("correct horse batterY", stored)


def test_the_same_password_hashes_differently_each_time():
    assert password.hash_password("same") != password.hash_password("same")


@pytest.mark.parametrize("stored", ["", "bcrypt$x", "scrypt$1$2$3$!!$!!", "scrypt$a$b$c$d$e"])
def test_a_malformed_hash_never_verifies(stored):
    assert not password.verify_password("anything", stored)


# --- authenticate ---------------------------------------------------------------


def _row(*, barred=False):
    return {**ALICE, "password_hash": password.hash_password("pw"), "barred": barred}


def test_authenticate_signs_in_and_clears_the_lockout(monkeypatch):
    conn = _fake_db(monkeypatch, _row())

    assert asyncio.run(password.authenticate("alice", "pw")) == ALICE
    ((sql, _),) = conn.updates
    assert "failed_attempts = 0" in sql and "last_sign_in_at = now()" in sql


def test_authenticate_counts_a_wrong_password(monkeypatch):
    conn = _fake_db(monkeypatch, _row())

    assert asyncio.run(password.authenticate("alice", "nope")) is None
    ((sql, args),) = conn.updates
    assert "locked_until" in sql
    assert args[1:] == (password.MAX_FAILED_ATTEMPTS, password.LOCKOUT)


def test_authenticate_checks_nothing_for_a_barred_account(monkeypatch):
    # Disabled or locked: refused even with the right password, and no
    # counter moves.
    conn = _fake_db(monkeypatch, _row(barred=True))

    assert asyncio.run(password.authenticate("alice", "pw")) is None
    assert conn.updates == []


def test_authenticate_refuses_an_unknown_account(monkeypatch):
    conn = _fake_db(monkeypatch, None)

    assert asyncio.run(password.authenticate("nobody", "pw")) is None
    assert conn.updates == []


# --- /password ------------------------------------------------------------------


def _password(client, login_name="alice", password="pw"):
    return client.post("/v1/admin/auth/password", json={"login_name": login_name, "password": password})


def _authenticate_as(monkeypatch, account):
    seen = {}

    async def authenticate(login, password):
        seen["args"] = (login, password)
        return account

    monkeypatch.setattr(password, "authenticate", authenticate)
    return seen


def test_password_issues_a_token_for_an_account(monkeypatch, client):
    seen = _authenticate_as(monkeypatch, ALICE)

    resp = _password(client, login_name="  alice  ")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert seen["args"] == ("alice", "pw")
    assert body["id_token"] is None
    assert body["expires_in"] == auth.LOCAL_TOKEN_SECONDS
    assert body["user"] == {
        "sub": "local:alice",
        "name": "Alice",
        "email": "alice@example.com",
        "login_name": "alice",
        "roles": ["platform_admin"],
        "org_id": None,
    }
    claims = jwt.decode(body["access_token"], SECRET, algorithms=["HS256"], audience=auth.LOCAL_AUDIENCE)
    assert claims["iss"] == auth.LOCAL_ISSUER and claims["sub"] == "local:alice"


def test_password_refuses_bad_credentials_with_one_message(monkeypatch, client):
    _authenticate_as(monkeypatch, None)

    resp = _password(client)
    assert resp.status_code == 401
    assert resp.json()["detail"] == password.BAD_CREDENTIALS


def test_password_is_off_without_a_secret(monkeypatch, client):
    monkeypatch.setattr(config, "ADMIN_LOCAL_TOKEN_SECRET", "")
    seen = _authenticate_as(monkeypatch, ALICE)

    assert _password(client).status_code == 503
    # Not even checked: a guess must not count towards a lockout when no
    # token could have been issued for it anyway.
    assert seen == {}


def test_password_needs_both_fields(client):
    assert _password(client, login_name=" ").status_code == 400
    assert _password(client, password="").status_code == 400


# --- the token, on the admin API -------------------------------------------------


def _account(*, disabled=False, changed=None):
    return {"disabled": disabled, "password_changed_at": changed or datetime(2020, 1, 1, tzinfo=timezone.utc)}


def _whoami(client, token):
    return client.get("/whoami", headers={"Authorization": f"Bearer {token}"})


def test_a_local_token_opens_the_admin_api(monkeypatch, client):
    _fake_db(monkeypatch, _account())

    resp = _whoami(client, auth.issue_local_token("alice"))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"actor": "local:alice"}


def test_a_disabled_account_is_out_at_once(monkeypatch, client):
    _fake_db(monkeypatch, _account(disabled=True))

    assert _whoami(client, auth.issue_local_token("alice")).status_code == 401


def test_a_deleted_account_is_out_at_once(monkeypatch, client):
    _fake_db(monkeypatch, None)

    assert _whoami(client, auth.issue_local_token("alice")).status_code == 401


def test_a_new_password_signs_out_older_tokens(monkeypatch, client):
    token = auth.issue_local_token("alice")
    _fake_db(monkeypatch, _account(changed=datetime.now(timezone.utc) + timedelta(minutes=1)))

    assert _whoami(client, token).status_code == 401


def test_a_forged_local_token_is_refused(monkeypatch, client):
    _fake_db(monkeypatch, _account())
    forged = jwt.encode(
        {"iss": auth.LOCAL_ISSUER, "aud": auth.LOCAL_AUDIENCE, "sub": "local:alice", "iat": 0, "exp": 2**31 - 1},
        "not-the-secret-not-the-secret-not-the-secret",
        algorithm="HS256",
    )

    assert _whoami(client, forged).status_code == 401


def test_a_local_token_opens_no_tenant_route(monkeypatch):
    # get_claims, in front of every tenant-facing route, takes Zitadel's
    # RS256 tokens only — this one names no key in Zitadel's JWKS.
    class NoKeys:
        def get_signing_key_from_jwt(self, token):
            raise jwt.PyJWKClientError("no such key")

    monkeypatch.setattr(auth, "_get_jwks_client", NoKeys)
    with pytest.raises(HTTPException) as refused:
        auth.verify_token(auth.issue_local_token("alice"))
    assert refused.value.status_code == 401
