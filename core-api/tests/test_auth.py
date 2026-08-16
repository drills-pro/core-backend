from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app import auth


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


def test_verify_token_extracts_org_and_roles(monkeypatch, keypair):
    private_key, public_key = keypair
    _patch_jwks(monkeypatch, public_key)
    token = _sign(
        private_key,
        {
            "sub": "user-1",
            "iss": auth.ZITADEL_ISSUER,
            auth.ORG_ID_CLAIM: "org-123",
            auth.ROLES_CLAIM: {"org_owner": {"org-123": "acme"}},
        },
    )

    claims = auth.verify_token(token)

    assert claims.subject == "user-1"
    assert claims.org_id == "org-123"
    assert claims.roles == {"org_owner"}


def test_verify_token_rejects_wrong_signature(monkeypatch, keypair):
    _, public_key = keypair
    other_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    _patch_jwks(monkeypatch, public_key)
    token = _sign(
        other_private_key,
        {"sub": "user-1", "iss": auth.ZITADEL_ISSUER, auth.ORG_ID_CLAIM: "org-123"},
    )

    with pytest.raises(Exception):
        auth.verify_token(token)


def test_verify_token_rejects_missing_org_claim(monkeypatch, keypair):
    private_key, public_key = keypair
    _patch_jwks(monkeypatch, public_key)
    token = _sign(private_key, {"sub": "user-1", "iss": auth.ZITADEL_ISSUER})

    with pytest.raises(Exception):
        auth.verify_token(token)
