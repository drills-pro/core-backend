"""The admin console's sign-in (app/modules/admin/auth), end to end against
a fake Zitadel: discovery is pre-seeded, the token endpoint is an
httpx.MockTransport, and tokens are signed with a throwaway RSA key."""
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import auth, config
from app.modules.admin.auth import routes, zitadel

CLIENT_ID = "admin-client"


@pytest.fixture
def private_key(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    fake_jwks = SimpleNamespace(get_signing_key_from_jwt=lambda _t: SimpleNamespace(key=key.public_key()))
    monkeypatch.setattr(auth, "_get_jwks_client", lambda: fake_jwks)
    monkeypatch.setattr(config, "ZITADEL_ADMIN_CLIENT_ID", CLIENT_ID)
    monkeypatch.setattr(config, "ZITADEL_ADMIN_CLIENT_SECRET", "s3cret")
    monkeypatch.setattr(
        zitadel,
        "_discovery",
        {
            "issuer": auth.ZITADEL_ISSUER,
            "authorization_endpoint": f"{auth.ZITADEL_ISSUER}/oauth/v2/authorize",
            "token_endpoint": f"{auth.ZITADEL_ISSUER}/oauth/v2/token",
            "end_session_endpoint": f"{auth.ZITADEL_ISSUER}/oidc/v1/end_session",
        },
    )
    return key


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app)


def _sign(key, **claims) -> str:
    return jwt.encode({"iss": auth.ZITADEL_ISSUER, "exp": 2**31 - 1, **claims}, key, algorithm="RS256")


def _fake_zitadel(monkeypatch, key, *, nonce_from_tx=True, roles=("platform_admin",)):
    """Points the token endpoint at a handler that checks what core-api
    sends and answers with tokens signed by `key`."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        seen["form"] = form
        seen["auth"] = request.headers.get("authorization")
        id_token = _sign(
            key,
            sub="u1",
            aud=CLIENT_ID,
            nonce=seen["nonce"] if nonce_from_tx else "someone-else",
            name="Ada",
            email="ada@example.com",
            preferred_username="ada",
        )
        access_token = _sign(
            key,
            sub="u1",
            **{auth.ORG_ID_CLAIM: "org-1", auth.ROLES_CLAIM: {r: {"org-1": "x"} for r in roles}},
        )
        return httpx.Response(200, json={"access_token": access_token, "id_token": id_token, "expires_in": 3600})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        zitadel.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    )
    return seen


BINDING = "b" * 43


def _start(client, seen):
    """GET /login as the console sends the browser there; the tx cookie
    lands in the client's jar, as it would in the browser's."""
    resp = client.get(
        "/v1/admin/auth/login",
        params={"binding": zitadel._binding_hash(BINDING)},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    params = parse_qs(urlparse(resp.headers["location"]).query)
    seen["nonce"] = params["nonce"][0]
    return params


def _callback(client, **params):
    return client.get("/v1/admin/auth/callback", params=params, follow_redirects=False)


def _landing(resp):
    """Where a /callback redirect sends the browser, and its query."""
    assert resp.status_code == 303, resp.text
    url = urlparse(resp.headers["location"])
    return url, {k: v[0] for k, v in parse_qs(url.query).items()}


def _sign_in(client, seen, **overrides):
    params = _start(client, seen)
    return _callback(client, code="abc", state=params["state"][0], **overrides)


def test_login_builds_a_pkce_request(private_key, client):
    params = _start(client, {})

    assert params["client_id"] == [CLIENT_ID]
    assert params["redirect_uri"] == [config.ZITADEL_ADMIN_REDIRECT_URI]
    assert params["code_challenge_method"] == ["S256"]
    sealed = client.cookies.get(zitadel.TX_COOKIE)
    # Sealed: nothing about the verifier is readable from the outside.
    assert sealed and "verifier" not in sealed


def test_callback_completes_a_sign_in(monkeypatch, private_key, client):
    seen = _fake_zitadel(monkeypatch, private_key)

    url, query = _landing(_sign_in(client, seen))
    assert f"{url.scheme}://{url.netloc}{url.path}" == f"{config.ADMIN_CONSOLE_URL}/auth/complete"
    # Spent once used.
    assert client.cookies.get(zitadel.TX_COOKIE) is None

    resp = client.post("/v1/admin/auth/redeem", json={"ticket": query["ticket"], "binding": BINDING})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "binding" not in body
    assert body["user"] == {
        "sub": "u1",
        "name": "Ada",
        "email": "ada@example.com",
        "login_name": "ada",
        "roles": ["platform_admin"],
        "org_id": "org-1",
    }
    # Confidential client: the secret goes to Zitadel, with the PKCE verifier.
    assert seen["auth"].startswith("Basic ")
    assert seen["form"]["code"] == ["abc"]
    assert len(seen["form"]["code_verifier"][0]) >= 43


def _refusal(resp) -> str:
    """The reason a refused /callback sends the browser to the console's
    /login with — never a ticket."""
    url, query = _landing(resp)
    assert f"{url.scheme}://{url.netloc}{url.path}" == f"{config.ADMIN_CONSOLE_URL}/login"
    assert "ticket" not in query
    return query["error"]


def test_callback_refuses_a_mismatched_state(monkeypatch, private_key, client):
    seen = _fake_zitadel(monkeypatch, private_key)
    _start(client, seen)

    assert "did not start here" in _refusal(_callback(client, code="abc", state="forged"))


def test_callback_refuses_a_browser_without_the_tx_cookie(monkeypatch, private_key, client):
    seen = _fake_zitadel(monkeypatch, private_key)
    params = _start(client, seen)
    client.cookies.clear()

    assert "expired" in _refusal(_callback(client, code="abc", state=params["state"][0]))


def test_callback_refuses_a_tampered_transaction(monkeypatch, private_key, client):
    seen = _fake_zitadel(monkeypatch, private_key)
    params = _start(client, seen)
    forged = zitadel.Fernet(zitadel.Fernet.generate_key()).encrypt(
        json.dumps({"state": params["state"][0], "nonce": "n", "verifier": "v", "binding": "x"}).encode()
    )
    client.cookies.clear()
    client.cookies.set(zitadel.TX_COOKIE, forged.decode(), path=zitadel.TX_COOKIE_PATH)

    assert "expired" in _refusal(_callback(client, code="abc", state=params["state"][0]))


def test_callback_passes_on_zitadels_own_refusal(private_key, client):
    _start(client, {})

    reason = _refusal(_callback(client, error="access_denied", error_description="User cancelled"))
    assert reason == "User cancelled"


def test_callback_refuses_an_id_token_from_another_sign_in(monkeypatch, private_key, client):
    seen = _fake_zitadel(monkeypatch, private_key, nonce_from_tx=False)

    assert "different sign-in" in _refusal(_sign_in(client, seen))


def test_callback_refuses_someone_without_platform_admin(monkeypatch, private_key, client):
    seen = _fake_zitadel(monkeypatch, private_key, roles=("org_owner",))

    assert "platform_admin" in _refusal(_sign_in(client, seen))


def test_redeem_refuses_another_browsers_binding(monkeypatch, private_key, client):
    seen = _fake_zitadel(monkeypatch, private_key)
    _, query = _landing(_sign_in(client, seen))

    resp = client.post("/v1/admin/auth/redeem", json={"ticket": query["ticket"], "binding": "c" * 43})
    assert resp.status_code == 400


def test_redeem_refuses_an_expired_ticket(monkeypatch, private_key, client):
    seen = _fake_zitadel(monkeypatch, private_key)
    _, query = _landing(_sign_in(client, seen))
    monkeypatch.setattr(zitadel, "TICKET_TTL", -1)

    resp = client.post("/v1/admin/auth/redeem", json={"ticket": query["ticket"], "binding": BINDING})
    assert resp.status_code == 400


def test_logout_only_sends_a_hint_zitadel_will_accept(private_key, client):
    valid = _sign(private_key, sub="u1", aud=CLIENT_ID)
    expired = jwt.encode(
        {"iss": auth.ZITADEL_ISSUER, "sub": "u1", "aud": CLIENT_ID, "exp": 1}, private_key, algorithm="RS256"
    )

    url = client.post("/v1/admin/auth/logout", json={"id_token": valid}).json()["end_session_url"]
    params = parse_qs(urlparse(url).query)
    assert params["id_token_hint"] == [valid]
    assert params["post_logout_redirect_uri"] == [config.ZITADEL_ADMIN_POST_LOGOUT_REDIRECT_URI]

    assert client.post("/v1/admin/auth/logout", json={"id_token": expired}).json() == {"end_session_url": None}
    assert client.post("/v1/admin/auth/logout", json={}).json() == {"end_session_url": None}

