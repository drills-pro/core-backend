"""Embedded email signup/login (req/02 FR1/FR5) — real Zitadel accounts,
real password checks, but the browser never navigates to Zitadel's own
domain. Only core-api talks to Zitadel directly (server-to-server, no
CORS/redirect constraints); the frontend only ever calls core-api.

Flow, per Zitadel's documented "Build your own Login UI" mechanism:
1. Create an OIDC auth request against our own confidential client
   (GET /oauth/v2/authorize, redirect not followed — just read the
   Location header for the authRequestId Zitadel generated).
2. Create a Session (POST /v2/sessions) with the user's password check —
   this *is* the login: Zitadel verifies the password here.
3. Finalize the auth request with that session
   (POST /v2/oidc/auth_requests/{id}) — Zitadel returns a callback URL
   pointing at *our own* registered redirect_uri with a real `code`.
4. Exchange that code for tokens the normal way (POST /oauth/v2/token).

Step 3's callback URL is the only place Zitadel's domain would appear,
and it's never navigated to — we just parse the code out of it server-side.
"""
import base64
import hashlib
import re
import secrets

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core import vault
from app.integrations import zitadel

router = APIRouter(prefix="/v1/auth", tags=["auth"])

ZITADEL_ISSUER = zitadel.ISSUER
REDIRECT_URI = "http://localhost:8090/auth/callback"  # never actually navigated to, see module docstring


def _zitadel_client_creds() -> tuple[str, str]:
    """client_id/client_secret for this app's own Zitadel OIDC (login) client.

    Unrelated to zitadel.auth_header()'s Service User credential above —
    this is a separate, plain-secret confidential client used only for this
    flow's own authorization_code exchange. Still a direct Vault KV read
    (ponytail: this one wasn't part of the disk-based-secrets move — the
    Service User key was the piece that needed fixing this round; move this
    to the same Vault Agent Injector file if/when it also needs it).
    """
    creds = vault.get_kv("platform/zitadel-oidc")
    return creds["client_id"], creds["client_secret"]


def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


class RegisterRequest(BaseModel):
    email: str
    password: str
    first_name: str
    last_name: str


class LoginRequest(BaseModel):
    email: str
    password: str


@router.post("/register", status_code=201)
async def register(body: RegisterRequest):
    """Real Zitadel account creation (req/02 FR5-adjacent: self-serve
    signup). Email is marked verified immediately — no SMTP is configured
    in this dev stack to send a real verification mail; a production
    deployment would set isVerified based on an actual sent/clicked link."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{ZITADEL_ISSUER}/v2/users/human",
            headers=zitadel.auth_header(),
            json={
                "profile": {"givenName": body.first_name, "familyName": body.last_name},
                "email": {"email": body.email, "isVerified": True},
                "password": {"password": body.password},
            },
        )
    if resp.status_code >= 400:
        raise HTTPException(status_code=400, detail=f"registration failed: {resp.text}")
    return {"userId": resp.json()["userId"]}


async def _create_auth_request(client: httpx.AsyncClient) -> str:
    client_id, _ = _zitadel_client_creds()
    verifier, challenge = _pkce_pair()
    resp = await client.get(
        f"{ZITADEL_ISSUER}/oauth/v2/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": "openid profile email urn:zitadel:iam:user:resourceowner",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
        follow_redirects=False,
    )
    location = resp.headers.get("location", "")
    match = re.search(r"authRequest=(V2_[\w-]+)", location)
    if not match:
        raise HTTPException(status_code=502, detail=f"could not start auth request: {location}")
    return match.group(1), verifier


@router.post("/login")
async def login(body: LoginRequest):
    async with httpx.AsyncClient(timeout=10) as client:
        auth_request_id, code_verifier = await _create_auth_request(client)

        session_resp = await client.post(
            f"{ZITADEL_ISSUER}/v2/sessions",
            headers=zitadel.auth_header(),
            json={"checks": {"user": {"loginName": body.email}, "password": {"password": body.password}}},
        )
        if session_resp.status_code >= 400:
            raise HTTPException(status_code=401, detail="invalid email or password")
        session = session_resp.json()

        finalize_resp = await client.post(
            f"{ZITADEL_ISSUER}/v2/oidc/auth_requests/{auth_request_id}",
            headers=zitadel.auth_header(),
            json={"session": {"sessionId": session["sessionId"], "sessionToken": session["sessionToken"]}},
        )
        if finalize_resp.status_code >= 400:
            raise HTTPException(status_code=502, detail=f"finalize failed: {finalize_resp.text}")
        callback_url = finalize_resp.json()["callbackUrl"]
        code_match = re.search(r"[?&]code=([^&]+)", callback_url)
        if not code_match:
            raise HTTPException(status_code=502, detail=f"no code in callback: {callback_url}")
        code = code_match.group(1)

        client_id, client_secret = _zitadel_client_creds()
        token_resp = await client.post(
            f"{ZITADEL_ISSUER}/oauth/v2/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "client_id": client_id,
                "client_secret": client_secret,
                "code_verifier": code_verifier,
            },
        )
        if token_resp.status_code >= 400:
            raise HTTPException(status_code=502, detail=f"token exchange failed: {token_resp.text}")
        tokens = token_resp.json()

    return {
        "access_token": tokens["access_token"],
        "id_token": tokens["id_token"],
        "expires_in": tokens["expires_in"],
    }
