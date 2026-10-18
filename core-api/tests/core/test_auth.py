from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.core import auth
from app.core.settings import settings

PROJECT = "proj-1"


@pytest.fixture(autouse=True)
def _project(monkeypatch):
    monkeypatch.setattr(settings.zitadel, "project_id", PROJECT)


@pytest.fixture
def keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


def _sign(private_key, claims: dict) -> str:
    return jwt.encode(claims, private_key, algorithm="RS256")


def _patch_jwks(monkeypatch, public_key):
    fake_client = SimpleNamespace(
        get_signing_key_from_jwt=lambda token: SimpleNamespace(key=public_key)
    )
    monkeypatch.setattr(auth, "_get_jwks_client", lambda: fake_client)


def test_verify_token_extracts_identity_and_roles(monkeypatch, keypair):
    private_key, public_key = keypair
    _patch_jwks(monkeypatch, public_key)
    token = _sign(
        private_key,
        {
            "sub": "user-1",
            "iss": auth.ZITADEL_ISSUER, "aud": [PROJECT], "iat": 1, "exp": 4102444800,
            "email": "a@b.co", "email_verified": True, "name": "Ann",
            auth.ROLES_CLAIM: {"org_owner": {"org-123": "acme"}},
        },
    )

    claims = auth.verify_token(token)

    assert claims.subject == "user-1"
    assert (claims.email, claims.email_verified, claims.display_name) == ("a@b.co", True, "Ann")
    assert claims.roles == {"org_owner"}


def test_verify_token_rejects_wrong_audience(monkeypatch, keypair):
    private_key, public_key = keypair
    _patch_jwks(monkeypatch, public_key)
    token = _sign(
        private_key,
        {"sub": "u", "iss": auth.ZITADEL_ISSUER, "aud": ["other"], "iat": 1, "exp": 4102444800},
    )
    with pytest.raises(Exception):
        auth.verify_token(token)


def test_dev_headers_rejected_unless_enabled(monkeypatch):
    import asyncio

    from fastapi import HTTPException

    from app.core import tenancy

    monkeypatch.setattr(settings.auth, "dev_headers", False)
    with pytest.raises(HTTPException) as e:
        asyncio.run(tenancy.get_tenant_id(authorization=None, x_tenant_id="t"))
    assert e.value.status_code == 401
    with pytest.raises(HTTPException):
        asyncio.run(tenancy.get_actor(authorization=None, x_user_id="u"))
    with pytest.raises(HTTPException):
        asyncio.run(auth.get_platform_admin(authorization=None, x_admin_user="a"))


def test_verify_token_rejects_wrong_signature(monkeypatch, keypair):
    _, public_key = keypair
    other_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    _patch_jwks(monkeypatch, public_key)
    token = _sign(
        other_private_key,
        {"sub": "user-1", "iss": auth.ZITADEL_ISSUER, "aud": [PROJECT], "iat": 1, "exp": 4102444800},
    )

    with pytest.raises(Exception):
        auth.verify_token(token)


def test_verify_token_rejects_missing_required_claims(monkeypatch, keypair):
    private_key, public_key = keypair
    _patch_jwks(monkeypatch, public_key)
    token = _sign(private_key, {"sub": "user-1", "iss": auth.ZITADEL_ISSUER})

    with pytest.raises(Exception):
        auth.verify_token(token)


class _Conn:
    def __init__(self, role, mine):
        self.role, self.mine = role, mine

    async def fetchval(self, sql, *args):
        return self.role

    async def fetch(self, sql, *args):
        return [{"id": t} for t in self.mine]


def _pool(monkeypatch, role=None, mine=()):
    from app.core import tenancy

    class Ctx:
        async def __aenter__(self):
            return _Conn(role, mine)

        async def __aexit__(self, *exc):
            return False

    async def get_pool():
        return SimpleNamespace(acquire=Ctx)

    monkeypatch.setattr(tenancy, "get_pool", get_pool)
    return tenancy


def _resolve(tenancy, requested):
    import asyncio

    return asyncio.run(tenancy._member_tenant("u", requested))


def test_member_tenant_resolution(monkeypatch):
    import uuid

    from fastapi import HTTPException

    t1, t2 = str(uuid.uuid4()), str(uuid.uuid4())
    # Header names a tenant the user is in.
    assert _resolve(_pool(monkeypatch, role="member"), t1) == t1
    # Header names a tenant the user is not in, or is not a uuid: 404, never 403.
    for bad in (t1, "nope"):
        with pytest.raises(HTTPException) as e:
            _resolve(_pool(monkeypatch, role=None), bad)
        assert e.value.status_code == 404
    # No header: one tenant is implied, none or several are not.
    assert _resolve(_pool(monkeypatch, mine=[t1]), None) == t1
    with pytest.raises(HTTPException) as e:
        _resolve(_pool(monkeypatch, mine=[]), None)
    assert e.value.status_code == 403
    with pytest.raises(HTTPException) as e:
        _resolve(_pool(monkeypatch, mine=[t1, t2]), None)
    assert e.value.status_code == 400


def test_jwks_client_uses_internal_url_with_public_host(monkeypatch):
    monkeypatch.setattr(auth, "_jwks_client", None)
    monkeypatch.setattr(auth, "ZITADEL_ISSUER", "https://auth.example.com")
    monkeypatch.setattr(settings.zitadel, "internal_url", "http://zitadel.zitadel.svc:8080")
    client = auth._get_jwks_client()
    assert client.uri == "http://zitadel.zitadel.svc:8080/oauth/v2/keys"
    assert client.headers["Host"] == "auth.example.com"
    monkeypatch.setattr(auth, "_jwks_client", None)


def test_profile_claims_come_from_userinfo_when_the_token_has_none(monkeypatch):
    import asyncio

    calls = []

    class FakeClient:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return False
        async def get(self, url, headers):
            calls.append((url, headers["Authorization"]))
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"email": "a@b.co", "email_verified": True, "name": "Ann"},
            )

    monkeypatch.setattr(auth.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(auth, "_userinfo_cache", {})
    monkeypatch.setattr(settings.zitadel, "internal_url", "http://z:8080")

    def fetch():
        return asyncio.run(auth.get_profile_claims(auth.Claims({"sub": "u"}, "tok")))

    claims = fetch()
    assert (claims.email, claims.email_verified, claims.display_name) == ("a@b.co", True, "Ann")
    assert calls == [("http://z:8080/oidc/v1/userinfo", "Bearer tok")]
    fetch()
    assert len(calls) == 1  # cached per token
    # A token that already carries the claims never calls out.
    asyncio.run(auth.get_profile_claims(auth.Claims({"sub": "u", "email": "x@y.z"}, "tok2")))
    assert len(calls) == 1
