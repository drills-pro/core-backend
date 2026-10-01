"""The admin console's sign-in: OIDC authorization code + PKCE against
Zitadel, with core-api as the confidential client *and* the redirect_uri.

core-api holds everything secret about the flow — the client secret, the
PKCE verifier, the nonce — and receives Zitadel's callback itself, so the
code is redeemed where it lands. The console only starts the browser off
and, at the end, collects the result:

    browser            console          core-api                       Zitadel
    /auth/login  ───►  binding cookie
                 ◄───  303 core-api /login?binding=H(b)
    GET /v1/admin/auth/login  ───────►  state, nonce, PKCE pair
                 ◄─────────────────────  303 authorize + tx cookie
    ────────────────────────────────────────────────────────────────►  login page
    GET /v1/admin/auth/callback?code&state  ◄────────────────────────  redirect
                                        tx cookie checked; code+secret ──► /oauth/v2/token
                 ◄─────────────────────  303 console /auth/complete?ticket
    /auth/complete ──► POST /redeem {ticket, b} ──► tokens, once H(b) matches

Two sealed values carry the state, so core-api stays stateless — no store
to share between replicas, nothing to clean up. Both are Fernet (AES +
HMAC, with its own timestamp for the TTL) under a key only core-api has:

- `admin_oidc_tx`, a cookie on core-api's own origin between /login and
  /callback: state, nonce, verifier and the console's binding hash.
- the `ticket`, carried to the console in the redirect: the tokens and the
  user, readable by nobody but core-api, and good for a minute.

The binding ties the ticket to the browser that started the sign-in: the
console keeps a random value in its own cookie and core-api only ever sees
its hash until /redeem. A ticket that leaks from a URL is useless anywhere
else, and nobody can sign a victim in as themselves by sending them one.

Only platform operators get a ticket out of /callback: the console is the
operator's back office, so a Zitadel account without the platform_admin
role is refused here, at sign-in, rather than on every page after it.
"""
import base64
import hashlib
import json
import logging
import secrets
from urllib.parse import urlencode

import httpx
import jwt
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app.core import config
from app.core.auth import (
    PLATFORM_ADMIN_ROLE,
    Claims,
    decode_zitadel_jwt,
    get_claims,
    verify_token,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/admin/auth", tags=["admin"])

# The last two are Zitadel reserved scopes: project roles asserted into the
# tokens, and the user's org (= tenant) as flat claims — the two things
# app.core.auth.verify_token reads off every access token.
SCOPES = " ".join(
    [
        "openid",
        "profile",
        "email",
        "urn:zitadel:iam:org:project:roles",
        "urn:zitadel:iam:user:resourceowner",
    ]
)
# Long enough to read Zitadel's login page, short enough not to linger.
TRANSACTION_TTL = 10 * 60
# Only has to survive one redirect and one server-to-server call.
TICKET_TTL = 60
ZITADEL_TIMEOUT = 10

# Named apart from the console's own cookies: in dev both run on localhost,
# and a browser does not tell cookies apart by port.
TX_COOKIE = "admin_oidc_tx"
TX_COOKIE_PATH = "/v1/admin/auth"

_discovery: dict | None = None


def _client() -> tuple[str, str]:
    if not (config.ZITADEL_ADMIN_CLIENT_ID and config.ZITADEL_ADMIN_CLIENT_SECRET):
        raise HTTPException(
            status_code=503,
            detail="admin sign-in is not configured: set ZITADEL_ADMIN_CLIENT_ID and ZITADEL_ADMIN_CLIENT_SECRET",
        )
    return config.ZITADEL_ADMIN_CLIENT_ID, config.ZITADEL_ADMIN_CLIENT_SECRET


def _fernet() -> Fernet:
    """Keyed off the client secret, so there is no second secret to
    provision. Rotating the client secret only voids sign-ins in flight."""
    _, secret = _client()
    key = hashlib.sha256(b"admin-auth-transaction:" + secret.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def _seal(payload: dict) -> str:
    return _fernet().encrypt(json.dumps(payload).encode()).decode()


def _unseal(sealed: str, ttl: int) -> dict:
    """Raises InvalidToken when expired, forged or not one of ours."""
    return json.loads(_fernet().decrypt(sealed.encode(), ttl=ttl))


def _binding_hash(binding: str) -> str:
    return hashlib.sha256(binding.encode()).hexdigest()


async def _endpoints() -> dict:
    """Zitadel's discovery document, fetched once per process. A failure is
    not cached, so the next sign-in tries again."""
    global _discovery
    if _discovery is None:
        url = f"{config.ZITADEL_ISSUER}/.well-known/openid-configuration"
        try:
            async with httpx.AsyncClient(timeout=ZITADEL_TIMEOUT) as client:
                resp = await client.get(url)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"could not reach Zitadel at {url}: {e}") from e
        doc = resp.json()
        # Every token is later trusted on this one string matching.
        if doc.get("issuer") != config.ZITADEL_ISSUER:
            raise HTTPException(
                status_code=502,
                detail=f"Zitadel calls itself {doc.get('issuer')}, but ZITADEL_ISSUER is {config.ZITADEL_ISSUER}",
            )
        _discovery = doc
    return _discovery


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _to_console(path: str, **params: str) -> RedirectResponse:
    """303 so the browser arrives with a GET whatever brought it here."""
    return RedirectResponse(f"{config.ADMIN_CONSOLE_URL}{path}?{urlencode(params)}", status_code=303)


def _refused(reason: str) -> RedirectResponse:
    """Back to the console's sign-in page with a reason it can show, and
    without the half-finished transaction — retrying should start clean."""
    response = _to_console("/login", error=reason)
    response.delete_cookie(TX_COOKIE, path=TX_COOKIE_PATH)
    return response


def _shown(e: HTTPException, fallback: str) -> str:
    """400/403 are about this sign-in (expired, refused, no role) and say
    so; anything else names endpoints and claims, which is for the log."""
    if e.status_code in (400, 403):
        return str(e.detail)
    logger.error("admin sign-in failed: %s", e.detail)
    return fallback


@router.get("/login")
async def login(binding: str):
    """Starts a sign-in: the browser arrives from the console's /auth/login
    and leaves for Zitadel, with the transaction in a cookie on this
    origin — the one Zitadel will send it back to."""
    if not 32 <= len(binding) <= 128:
        return _refused("The sign-in link is malformed. Please try again.")
    try:
        client_id, _ = _client()
        endpoints = await _endpoints()
    except HTTPException as e:
        return _refused(_shown(e, "Could not start the sign-in."))

    state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
    params = {
        "client_id": client_id,
        "redirect_uri": config.ZITADEL_ADMIN_REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
        "nonce": nonce,
        "code_challenge": _pkce_challenge(verifier),
        "code_challenge_method": "S256",
    }
    response = RedirectResponse(f"{endpoints['authorization_endpoint']}?{urlencode(params)}", status_code=303)
    response.set_cookie(
        TX_COOKIE,
        _seal({"state": state, "nonce": nonce, "verifier": verifier, "binding": binding}),
        max_age=TRANSACTION_TTL,
        path=TX_COOKIE_PATH,
        httponly=True,
        # Not "strict": the browser comes back from Zitadel on a cross-site
        # redirect, and a strict cookie is not sent on one.
        samesite="lax",
        # Dev runs on plain http://localhost, where a secure cookie is dropped.
        secure=config.ZITADEL_ADMIN_REDIRECT_URI.startswith("https://"),
    )
    return response


@router.get("/callback")
async def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
):
    """Zitadel's redirect_uri. Redeems the code and hands the console a
    ticket for the result — or sends it back to /login with the reason,
    since a browser is on the other end and a JSON error would strand it."""
    # Zitadel reports its own refusals here rather than at the token
    # endpoint: the visitor cancelled, or the app may not ask what we asked.
    if error:
        return _refused(error_description or error)

    sealed = request.cookies.get(TX_COOKIE)
    if not sealed:
        return _refused("The sign-in expired. Please try again.")
    try:
        tx = _unseal(sealed, TRANSACTION_TTL)
    except InvalidToken:
        return _refused("The sign-in expired. Please try again.")

    if not code or not state:
        return _refused("Zitadel came back without an authorization code.")
    # What ties the callback to *this browser's* cookie: a code and state
    # replayed from anywhere else arrive without it.
    if not secrets.compare_digest(tx["state"], state):
        return _refused("That sign-in did not start here.")

    try:
        signed_in = await _redeem_code(code, tx)
    except HTTPException as e:
        return _refused(_shown(e, "Could not complete the sign-in."))

    response = _to_console("/auth/complete", ticket=_seal({"binding": tx["binding"], **signed_in}))
    # Spent: leaving it would let a replayed callback reuse this state.
    response.delete_cookie(TX_COOKIE, path=TX_COOKIE_PATH)
    return response


async def _redeem_code(code: str, tx: dict) -> dict:
    """Redeems the code, and answers with tokens only once the ID token is
    this sign-in's and the person is a platform admin."""
    client_id, client_secret = _client()
    endpoints = await _endpoints()
    async with httpx.AsyncClient(timeout=ZITADEL_TIMEOUT) as client:
        resp = await client.post(
            endpoints["token_endpoint"],
            auth=(client_id, client_secret),  # client_secret_basic
            data={
                "grant_type": "authorization_code",
                "code": code,
                # Must equal the one the code was issued for.
                "redirect_uri": config.ZITADEL_ADMIN_REDIRECT_URI,
                "code_verifier": tx["verifier"],
            },
        )
    if resp.status_code >= 400:
        raise HTTPException(status_code=400, detail=f"Zitadel refused the code: {resp.text}")
    tokens = resp.json()

    try:
        id_claims = decode_zitadel_jwt(tokens["id_token"], audience=client_id)
    except jwt.PyJWTError as e:
        raise HTTPException(status_code=502, detail=f"Zitadel's ID token did not verify: {e}") from e
    if id_claims.get("nonce") != tx["nonce"]:
        raise HTTPException(status_code=400, detail="ID token belongs to a different sign-in")

    # The access token is what every /v1/admin call will be checked with,
    # so the role is checked on it — not on the ID token — to refuse here
    # exactly whom get_platform_admin would refuse later.
    try:
        claims = verify_token(tokens["access_token"])
    except HTTPException as e:
        raise HTTPException(
            status_code=502,
            detail=f"access token unusable ({e.detail}) — set the Zitadel app's Auth Token Type to JWT",
        ) from e
    if PLATFORM_ADMIN_ROLE not in claims.roles:
        raise HTTPException(status_code=403, detail=f"this account does not hold the {PLATFORM_ADMIN_ROLE} role")

    return {
        "access_token": tokens["access_token"],
        "id_token": tokens["id_token"],
        "expires_in": tokens["expires_in"],
        "user": {
            "sub": claims.subject,
            "name": id_claims.get("name"),
            "email": id_claims.get("email"),
            "login_name": id_claims.get("preferred_username"),
            "roles": sorted(claims.roles),
            "org_id": claims.org_id,
        },
    }


class RedeemRequest(BaseModel):
    ticket: str
    binding: str


@router.post("/redeem")
async def redeem(body: RedeemRequest):
    """The console, server to server, trading the ticket /callback sent its
    browser for the tokens — once it shows the binding that hashes to the
    one the sign-in started with."""
    try:
        ticket = _unseal(body.ticket, TICKET_TTL)
    except InvalidToken:
        raise HTTPException(status_code=400, detail="sign-in expired or was tampered with; start again") from None
    if not secrets.compare_digest(ticket.pop("binding"), _binding_hash(body.binding)):
        raise HTTPException(status_code=400, detail="that sign-in did not start in this browser")
    return ticket


class LogoutRequest(BaseModel):
    id_token: str | None = None


@router.post("/logout")
async def logout(body: LogoutRequest):
    """Where to send the browser to end Zitadel's own session, or null.

    Zitadel needs a still-valid id_token_hint to honour
    post_logout_redirect_uri: a stale one gets a 400 page, and none at all
    strands the browser on Zitadel's "logged out" page. So the URL is only
    built when the hint will be accepted; otherwise the console just drops
    its own session and goes straight to its sign-in page."""
    client_id, _ = _client()
    if not body.id_token:
        return {"end_session_url": None}
    try:
        decode_zitadel_jwt(body.id_token, audience=client_id)
    except jwt.PyJWTError:
        return {"end_session_url": None}

    endpoints = await _endpoints()
    params = {
        "client_id": client_id,
        "id_token_hint": body.id_token,
        "post_logout_redirect_uri": config.ZITADEL_ADMIN_POST_LOGOUT_REDIRECT_URI,
    }
    return {"end_session_url": f"{endpoints['end_session_endpoint']}?{urlencode(params)}"}


@router.get("/me")
async def me(claims: Claims = Depends(get_claims)):
    """Who a bearer token says its holder is — for checking a token by hand."""
    if PLATFORM_ADMIN_ROLE not in claims.roles:
        raise HTTPException(status_code=403, detail=f"requires the {PLATFORM_ADMIN_ROLE} role")
    return {"sub": claims.subject, "org_id": claims.org_id, "roles": sorted(claims.roles)}
