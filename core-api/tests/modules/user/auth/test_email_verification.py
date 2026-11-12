"""GET /v1/me/email-verification reads the caller's email state live from Zitadel; POST /v1/me/email-verification
has Zitadel mail the verification link when the email is not verified. Zitadel and the rate limiter are faked."""
import httpx
import pytest

from app.core.auth import Claims, get_claims, get_profile_claims
from app.core.settings import settings
from app.modules.user.auth import routes, service
from app.modules.user.auth.constants import ZITADEL_VERIFY_URL_TEMPLATE, ProblemCode
from tests.support import make_client

URL = "/v1/me/email-verification"
CALLER = {"sub": "312", "email": "new@b.co", "email_verified": False}


@pytest.fixture
def world(monkeypatch):
    """Fakes and what they saw; `allow`, `up` and `verified` steer them."""
    w = type("World", (), {})()
    w.allow, w.up, w.verified = True, True, False
    w.looked_up, w.sent = [], []

    async def check(key, limit):
        return w.allow

    async def email_verified(client, user_id):
        if not w.up:
            raise httpx.ConnectError("down")
        w.looked_up.append(user_id)
        return w.verified

    async def send_email_verification(client, user_id, url_template):
        if not w.up:
            raise httpx.ConnectError("down")
        w.sent.append((user_id, url_template))

    monkeypatch.setattr(routes.rate_limit, "check", check)
    monkeypatch.setattr(service.zitadel, "email_verified", email_verified)
    monkeypatch.setattr(service.zitadel, "send_email_verification", send_email_verification)
    return w


def _client(raw=None):
    client = make_client(routes.router, headers={})
    if raw is not None:
        client.app.dependency_overrides[get_profile_claims] = lambda: Claims(raw)
        client.app.dependency_overrides[get_claims] = lambda: Claims(raw)
    return client


def test_an_unverified_email_gets_a_mail_from_zitadel_linking_to_its_login_verify_page(world):
    assert _client(CALLER).post(URL).status_code == 204
    assert world.looked_up == ["312"]
    [(user, template)] = world.sent
    assert user == "312" and template == settings.zitadel.issuer + ZITADEL_VERIFY_URL_TEMPLATE
    assert "/ui/v2/login/verify?" in template and "{{.UserID}}" in template and "{{.Code}}" in template


def test_an_email_zitadel_already_verified_is_409_even_if_the_token_says_otherwise(world):
    world.verified = True
    r = _client(CALLER).post(URL)
    assert (r.status_code, r.json()["code"]) == (409, ProblemCode.EMAIL_ALREADY_VERIFIED) and not world.sent


def test_an_identity_without_email_gets_409_no_email_and_zitadel_is_not_asked(world):
    r = _client({"sub": "312"}).post(URL)
    assert (r.status_code, r.json()["code"]) == (409, ProblemCode.NO_EMAIL) and not world.looked_up


def test_a_second_send_within_the_minute_is_429_and_reaches_nobody(world):
    world.allow = False
    r = _client(CALLER).post(URL)
    assert (r.status_code, r.json()["code"]) == (429, ProblemCode.TOO_MANY_ATTEMPTS)
    assert not world.looked_up and not world.sent


def test_zitadel_down_on_send_is_502_identity_unavailable(world):
    world.up = False
    r = _client(CALLER).post(URL)
    assert (r.status_code, r.json()["code"]) == (502, ProblemCode.IDENTITY_UNAVAILABLE)


def test_a_failed_send_after_the_lookup_is_502_too(world, monkeypatch):
    async def refuse(client, user_id, url_template):
        raise httpx.HTTPStatusError("no", request=httpx.Request("POST", "x"), response=httpx.Response(400))

    monkeypatch.setattr(service.zitadel, "send_email_verification", refuse)
    r = _client(CALLER).post(URL)
    assert (r.status_code, r.json()["code"]) == (502, ProblemCode.IDENTITY_UNAVAILABLE)


@pytest.mark.parametrize("verified", [False, True])
def test_status_is_zitadels_live_answer(world, verified):
    world.verified = verified
    r = _client(CALLER).get(URL)
    assert (r.status_code, r.json()) == (200, {"verified": verified}) and world.looked_up == ["312"]


def test_status_polling_is_capped(world):
    world.allow = False
    assert _client(CALLER).get(URL).status_code == 429 and not world.looked_up


def test_status_with_zitadel_down_is_502(world):
    world.up = False
    assert _client(CALLER).get(URL).json()["code"] == ProblemCode.IDENTITY_UNAVAILABLE


@pytest.mark.parametrize("method", ["get", "post"])
def test_both_need_a_token(world, method):
    assert getattr(_client(), method)(URL).status_code == 401
