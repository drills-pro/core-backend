"""The Zitadel management client: how it authenticates (PAT file or cached JWT-profile token) and what each call sends and returns."""
import asyncio
import json

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.core.enums import IdentityStatus
from app.integrations import zitadel

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIVATE_PEM = KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
PUBLIC = KEY.public_key()
CREDS = {"USER_ID": "svc-1", "KEY_ID": "kid-1", "PRIVATE_KEY": PRIVATE_PEM}


@pytest.fixture(autouse=True)
def fresh_caches(monkeypatch):
    monkeypatch.setattr(zitadel, "_pat_cache", None)
    monkeypatch.setattr(zitadel, "_token_cache", None)


class TokenEndpoint:
    """Replaces httpx.post for the token exchange."""

    def __init__(self, status=200, body=None):
        self.status, self.body, self.posts = status, body or {"access_token": "tok-1", "expires_in": 3600}, []

    def __call__(self, url, data, timeout):
        self.posts.append((url, data, timeout))
        return httpx.Response(self.status, json=self.body, request=httpx.Request("POST", url))


@pytest.fixture
def jwt_profile(monkeypatch):
    monkeypatch.setattr(zitadel, "_read_pat_file", lambda: None)
    monkeypatch.setattr(zitadel.secret_files, "get", lambda name: CREDS)
    endpoint = TokenEndpoint()
    monkeypatch.setattr(zitadel.httpx, "post", endpoint)
    return endpoint


# --- how the process authenticates --------------------------------------------------------------------------------

def test_the_pat_file_is_read_stripped_and_missing_is_none(tmp_path, monkeypatch):
    pat = tmp_path / "x.pat"
    pat.write_text("  secret-pat\n")
    monkeypatch.setattr(zitadel, "PAT_PATH", str(pat))
    assert zitadel._read_pat_file() == "secret-pat"
    monkeypatch.setattr(zitadel, "PAT_PATH", str(tmp_path / "missing.pat"))
    assert zitadel._read_pat_file() is None


def test_a_local_pat_wins_and_is_read_once(monkeypatch):
    reads = []
    monkeypatch.setattr(zitadel, "_read_pat_file", lambda: reads.append(1) or "local-pat")
    monkeypatch.setattr(zitadel.httpx, "post", lambda *a, **k: pytest.fail("token exchange despite a PAT"))
    assert zitadel.auth_header() == {"Authorization": "Bearer local-pat"}
    assert zitadel.get_pat() == "local-pat" and reads == [1]


def test_the_assertion_is_a_short_lived_jwt_signed_with_the_service_key(monkeypatch):
    monkeypatch.setattr(zitadel.secret_files, "get", lambda name: CREDS)
    token = zitadel._build_assertion()
    assert jwt.get_unverified_header(token) == {"alg": "RS256", "typ": "JWT", "kid": "kid-1"}
    claims = jwt.decode(token, PUBLIC, algorithms=["RS256"], audience=zitadel.ISSUER)
    assert (claims["iss"], claims["sub"]) == ("svc-1", "svc-1") and claims["exp"] - claims["iat"] == 60


def test_without_a_pat_the_jwt_profile_token_is_exchanged_once_and_cached(jwt_profile):
    assert zitadel.get_pat() == "tok-1" and zitadel.get_pat() == "tok-1"
    (url, data, timeout), = jwt_profile.posts
    assert url == f"{zitadel.ISSUER}/oauth/v2/token" and timeout == zitadel.REQUEST_TIMEOUT_SECONDS
    assert data["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer" and data["scope"] == zitadel._MANAGEMENT_SCOPE
    assert jwt.decode(data["assertion"], PUBLIC, algorithms=["RS256"], audience=zitadel.ISSUER)["iss"] == "svc-1"


def test_a_token_is_replaced_once_it_is_close_to_expiry(jwt_profile, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(zitadel.time, "monotonic", lambda: clock[0])
    assert zitadel.get_pat() == "tok-1"
    assert zitadel._token_cache[1] == 1000.0 + 3600 - 60  # refreshed a minute early
    jwt_profile.body = {"access_token": "tok-2", "expires_in": 3600}
    clock[0] = 1000.0 + 3539
    assert zitadel.get_pat() == "tok-1"
    clock[0] = 1000.0 + 3540
    assert zitadel.get_pat() == "tok-2" and len(jwt_profile.posts) == 2


def test_a_refused_token_exchange_raises_and_caches_nothing(jwt_profile):
    jwt_profile.status = 401
    with pytest.raises(httpx.HTTPStatusError):
        zitadel.get_pat()
    assert zitadel._token_cache is None


# --- the org-level calls ------------------------------------------------------------------------------------------

class Api:
    """An httpx client over a handler; `calls` collects (method, path, org header, json body)."""

    def __init__(self, handler):
        self.calls, self.auth = [], []

        def wrapped(request):
            self.auth.append(request.headers["authorization"])
            self.calls.append((request.method, request.url.path, request.headers.get("x-zitadel-orgid"), json.loads(request.content or b"null")))
            return handler(request)

        self.client = httpx.AsyncClient(transport=httpx.MockTransport(wrapped))


def run(call, handler):
    zitadel._pat_cache = "pat-1"  # restored by fresh_caches
    api = Api(handler)

    async def go():
        async with api.client as client:
            return await call(client)

    return asyncio.run(go()), api


def ok(body):
    return lambda request: httpx.Response(200, json=body)


def refuse(status):
    return lambda request: httpx.Response(status, json={"message": "no"})


def down(request):
    raise httpx.ConnectError("zitadel down")


def test_the_http_client_has_the_request_timeout():
    assert zitadel.client().timeout.read == zitadel.REQUEST_TIMEOUT_SECONDS


def test_org_states_map_zitadels_states_and_use_the_bearer_token():
    states, api = run(zitadel.list_org_states, ok({"result": [
        {"id": "a", "state": "ORG_STATE_ACTIVE"}, {"id": "b", "state": "ORG_STATE_INACTIVE"}, {"id": "c", "state": "ORG_STATE_REMOVED"}, {"id": "d"}]}))
    assert states == {"a": IdentityStatus.ACTIVE, "b": IdentityStatus.SUSPENDED, "c": IdentityStatus.UNKNOWN, "d": IdentityStatus.UNKNOWN}
    assert api.calls == [("POST", "/admin/v1/orgs/_search", None, {"query": {"limit": 1000}})]
    assert api.auth == ["Bearer pat-1"]


@pytest.mark.parametrize("handler", [down, refuse(500), refuse(403), ok({"result": None})])
def test_org_states_degrade_to_empty_instead_of_failing(handler):
    states, _ = run(zitadel.list_org_states, handler)
    assert states == {}


def test_org_users_carry_name_email_login_and_a_status_per_state():
    body = {"result": [
        {"id": "u1", "preferredLoginName": "ann@x", "state": "USER_STATE_ACTIVE", "human": {"profile": {"displayName": "Ann"}, "email": {"email": "ann@mail"}}},
        {"id": "u2", "preferredLoginName": "bob@x", "state": "USER_STATE_INITIAL", "human": {}},
        {"id": "u3", "preferredLoginName": "bot", "state": "USER_STATE_LOCKED"},
        {"id": "u4", "state": "USER_STATE_INACTIVE"},
        {"id": "u5", "state": "USER_STATE_DELETED"},
    ]}
    users, api = run(lambda c: zitadel.list_org_users(c, "o1"), ok(body))
    assert api.calls[0][1:3] == ("/management/v1/users/_search", "o1")
    assert (users["u1"].display_name, users["u1"].email, users["u1"].status) == ("Ann", "ann@mail", IdentityStatus.ACTIVE)
    assert (users["u2"].display_name, users["u2"].email, users["u2"].status) == ("bob@x", None, IdentityStatus.PENDING)  # falls back to the login
    assert users["u3"].status == IdentityStatus.SUSPENDED and users["u4"].status == IdentityStatus.SUSPENDED
    assert (users["u4"].display_name, users["u5"].status) == (None, IdentityStatus.UNKNOWN)


@pytest.mark.parametrize("org_id", [None, ""])
def test_an_org_less_tenant_has_no_users_and_asks_nobody(org_id):
    users, api = run(lambda c: zitadel.list_org_users(c, org_id), ok({}))
    assert users == {} and api.calls == []


@pytest.mark.parametrize("handler", [down, refuse(403)])
def test_org_users_degrade_to_empty_when_zitadel_cannot_answer(handler):
    assert run(lambda c: zitadel.list_org_users(c, "o1"), handler)[0] == {}


def test_create_org_posts_the_name_and_returns_the_id():
    org_id, api = run(lambda c: zitadel.create_org(c, "Acme"), ok({"id": "org-7"}))
    assert org_id == "org-7" and api.calls == [("POST", "/management/v1/orgs", None, {"name": "Acme"})]


def test_create_org_failures_are_raised_not_guessed_about():
    with pytest.raises(httpx.HTTPStatusError):
        run(lambda c: zitadel.create_org(c, "Acme"), refuse(409))
    with pytest.raises(httpx.ConnectError):
        run(lambda c: zitadel.create_org(c, "Acme"), down)


def test_delete_org_sends_a_delete_and_raises_on_refusal():
    _, api = run(lambda c: zitadel.delete_org(c, "org-7"), ok({}))
    assert api.calls[0][:2] == ("DELETE", "/admin/v1/orgs/org-7")
    with pytest.raises(httpx.HTTPStatusError):
        run(lambda c: zitadel.delete_org(c, "org-7"), refuse(404))


@pytest.mark.parametrize("active, action", [(True, "_reactivate"), (False, "_deactivate")])
def test_set_org_state_targets_the_org_through_its_header(active, action):
    _, api = run(lambda c: zitadel.set_org_state(c, "o1", active), ok({}))
    assert api.calls == [("POST", f"/management/v1/orgs/me/{action}", "o1", {})]


def test_set_org_state_raises_so_a_suspension_never_silently_does_nothing():
    with pytest.raises(httpx.HTTPStatusError):
        run(lambda c: zitadel.set_org_state(c, "o1", False), refuse(403))


HUMAN = {"userId": "h", "userType": "TYPE_HUMAN", "roles": ["ORG_OWNER"], "displayName": "Ada", "preferredLoginName": "ada@x"}
MACHINE = {"userId": "m", "userType": "TYPE_MACHINE", "roles": ["ORG_OWNER"], "preferredLoginName": "bot"}
VIEWER = {"userId": "v", "userType": "TYPE_HUMAN", "roles": ["ORG_PROJECT_CREATOR"]}


def owner(members):
    return run(lambda c: zitadel.get_org_owner(c, "o1"), ok({"result": members}))[0]


def test_the_owner_is_the_human_org_owner_even_when_the_machine_is_listed_first():
    assert owner([MACHINE, VIEWER, HUMAN]) == zitadel.OrgOwner(user_id="h", display_name="Ada", login_name="ada@x")


def test_with_no_human_owner_the_machine_user_is_named():
    assert owner([VIEWER, MACHINE]) == zitadel.OrgOwner(user_id="m", display_name="bot", login_name="bot")


@pytest.mark.parametrize("members", [[], [VIEWER], None])
def test_an_org_without_an_owner_has_none(members):
    assert owner(members) is None


@pytest.mark.parametrize("handler", [down, refuse(403)])
def test_the_owner_lookup_degrades_to_none(handler):
    assert run(lambda c: zitadel.get_org_owner(c, "o1"), handler)[0] is None


@pytest.mark.parametrize("org_id", [None, ""])
def test_an_org_less_tenant_has_no_owner_and_asks_nobody(org_id):
    found, api = run(lambda c: zitadel.get_org_owner(c, org_id), ok({}))
    assert found is None and api.calls == []


# --- email verification ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("body, verified", [
    ({"user": {"human": {"email": {"email": "a@b.co", "isVerified": True}}}}, True),
    ({"user": {"human": {"email": {"email": "a@b.co"}}}}, False),
    ({"user": {"machine": {}}}, False),
])
def test_email_verified_reads_the_users_current_email_state(body, verified):
    out, api = run(lambda c: zitadel.email_verified(c, "u1"), ok(body))
    assert out is verified
    assert api.calls == [("GET", "/v2/users/u1", None, None)] and api.auth == ["Bearer pat-1"]


@pytest.mark.parametrize("handler", [refuse(404), refuse(500), down])
def test_email_verified_raises_when_zitadel_cannot_answer(handler):
    with pytest.raises(httpx.HTTPError):
        run(lambda c: zitadel.email_verified(c, "u1"), handler)


def test_send_email_verification_has_zitadel_mail_its_link_template():
    template = "https://auth.example/ui/v2/login/verify?userId={{.UserID}}&code={{.Code}}"
    _, api = run(lambda c: zitadel.send_email_verification(c, "u1", template), ok({"details": {}}))
    assert api.calls == [("POST", "/v2/users/u1/email/send", None, {"sendCode": {"urlTemplate": template}})]


@pytest.mark.parametrize("handler", [refuse(400), refuse(503), down])
def test_send_email_verification_raises_when_zitadel_refuses(handler):
    with pytest.raises(httpx.HTTPError):
        run(lambda c: zitadel.send_email_verification(c, "u1", "t"), handler)
