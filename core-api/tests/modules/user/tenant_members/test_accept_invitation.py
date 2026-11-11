"""POST /v1/invitations/{token}:accept: a verified e-mail is required (anything else is the one 404 body); the service gets the token, e-mail and subject."""
import pytest

from app.core.auth import Claims, get_profile_claims
from app.modules.user.tenant_members import routes, service
from tests.support import TENANT, make_client

URL = "/v1/invitations/tok:accept"


@pytest.fixture
def accept(monkeypatch):
    seen = []

    async def fake(token, email, sub):
        seen.append((token, email, sub))
        return {"tenant_id": TENANT, "role": "member"}

    monkeypatch.setattr(service, "accept", fake)

    def go(raw):
        client = make_client(routes.router, headers={})
        client.app.dependency_overrides[get_profile_claims] = lambda: Claims(raw)
        return client.post(URL), seen

    return go


def test_a_verified_email_accepts_and_the_service_gets_token_email_and_subject(accept):
    r, seen = accept({"sub": "u9", "email": "a@b.co", "email_verified": True})
    assert (r.status_code, r.json()) == (200, {"tenant_id": TENANT, "role": "member"})
    assert seen == [("tok", "a@b.co", "u9")]


@pytest.mark.parametrize("raw", [{"sub": "u9", "email": "a@b.co"}, {"sub": "u9", "email_verified": True}])
def test_an_unverified_or_missing_email_is_404_and_nothing_is_accepted(accept, raw):
    r, seen = accept(raw)
    assert r.status_code == 404 and not seen
