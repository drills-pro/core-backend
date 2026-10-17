"""Zitadel management-API access shared by the modules that talk to it.

Local dev (self-hosted Zitadel via docker-compose) auto-provisions a
machine user + long-lived PAT on first init (see
compose/zitadel/firstinstance.yaml), mounted from the `zitadel-pat` volume
— read once, lazily, since it does not exist yet at import time on a cold
stack.

The cluster's Zitadel Cloud org has no such auto-provisioning. Instead this
app authenticates as a **Service User** via RFC 7523 JWT Profile: it
self-signs a short-lived assertion with a Zitadel-issued RSA key
(a mounted JSON file, see app/core/secret_files.py) and
exchanges it for a bearer token, caching that token until shortly before
it expires. It is a machine identity granted org-management rights, for
calling Zitadel's Management/Auth v2 APIs directly.

The admin back-office authenticates as this token; before the modular split
the latter reached into the former's private `_get_pat`, which is what
this module replaces.
"""
import logging
import time

import httpx
import jwt

from app.core import secret_files
from app.core.settings import settings

logger = logging.getLogger(__name__)

ISSUER = settings.zitadel.issuer
PAT_PATH = "/pat/zitadel-automation.pat"
# Reserved scope that authorizes a JWT-Profile token for Zitadel's own
# Management/Auth APIs (not a placeholder — see Zitadel's "JWT Profile"
# docs under Service Users).
_MANAGEMENT_SCOPE = "openid urn:zitadel:iam:org:project:id:zitadel:aud"

_pat_cache: str | None = None
_token_cache: tuple[str, float] | None = None  # (access_token, expires_at monotonic)


def _read_pat_file() -> str | None:
    try:
        with open(PAT_PATH) as f:
            return f.read().strip()
    except FileNotFoundError:
        return None


def _build_assertion() -> str:
    now = int(time.time())
    creds = secret_files.get("zitadel")
    return jwt.encode(
        {
            "iss": creds["USER_ID"],
            "sub": creds["USER_ID"],
            "aud": ISSUER,
            "iat": now,
            "exp": now + 60,
        },
        creds["PRIVATE_KEY"],
        algorithm="RS256",
        headers={"kid": creds["KEY_ID"]},
    )


def _fetch_jwt_profile_token() -> tuple[str, float]:
    # ponytail: sync httpx call inside otherwise-async request handlers —
    # acceptable since this only runs once per cache lifetime (tens of
    # minutes), not per-request. Swap for an async prefetch loop if that
    # assumption stops holding (e.g. very short-lived Zitadel tokens).
    resp = httpx.post(
        f"{ISSUER}/oauth/v2/token",
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": _build_assertion(),
            "scope": _MANAGEMENT_SCOPE,
        },
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["access_token"], time.monotonic() + data["expires_in"] - 60


def get_pat() -> str:
    global _pat_cache, _token_cache
    if _pat_cache is None:
        _pat_cache = _read_pat_file() or ""
    if _pat_cache:
        return _pat_cache
    if _token_cache is None or time.monotonic() >= _token_cache[1]:
        _token_cache = _fetch_jwt_profile_token()
    return _token_cache[0]


def auth_header() -> dict[str, str]:
    return {"Authorization": f"Bearer {get_pat()}"}


# --- Org-level reads for the back-office (app/modules/admin) ---------------
# Zitadel owns org state and org membership; core-api's `tenants` table only
# holds the mapping to them (init migration: "org maps 1:1 to tenant"). Rather than
# shadow-copying either into Postgres, the admin list reads them here.

ORG_STATE_TO_STATUS = {"ORG_STATE_ACTIVE": "active", "ORG_STATE_INACTIVE": "suspended"}
# Machine users are automation, not people: scripts/zitadel-bootstrap.sh's own
# PAT user holds ORG_OWNER on every org it provisions, so it would otherwise
# show up as the owner of everything.
_HUMAN = "TYPE_HUMAN"


async def list_org_states(client: httpx.AsyncClient) -> dict[str, str]:
    """{zitadel_org_id: 'active' | 'suspended'} for every org on the instance.

    One instance-level call, not one per org — the admin list would
    otherwise fan out across every tenant just to colour a status pill.
    Returns {} if Zitadel is unreachable; callers treat a missing org as
    unknown rather than failing the whole listing.
    """
    try:
        resp = await client.post(
            f"{ISSUER}/admin/v1/orgs/_search",
            headers=auth_header(),
            json={"query": {"limit": 1000}},
        )
        resp.raise_for_status()
    except (httpx.HTTPError, OSError):
        logger.warning("zitadel org search failed; tenant statuses unknown", exc_info=True)
        return {}
    return {
        org["id"]: ORG_STATE_TO_STATUS.get(org.get("state"), "unknown")
        for org in resp.json().get("result") or []
    }


USER_STATE_TO_STATUS = {
    "USER_STATE_ACTIVE": "active",
    "USER_STATE_INACTIVE": "suspended",
    "USER_STATE_LOCKED": "suspended",
    # Invited but never signed in: a real state of its own, not "active".
    "USER_STATE_INITIAL": "pending",
}


async def list_org_users(client: httpx.AsyncClient, org_id: str) -> dict[str, dict]:
    """{user_id: {display_name, email, login_name, status}} for one org.

    One call for the whole org rather than a lookup per member: the
    back-office joins this onto its own project_members rows, which only
    hold the Zitadel subject. Returns {} when the org is gone or Zitadel is
    down, and callers fall back to showing the bare user id.
    """
    try:
        resp = await client.post(
            f"{ISSUER}/management/v1/users/_search",
            headers={**auth_header(), "x-zitadel-orgid": org_id},
            json={"query": {"limit": 1000}},
        )
        resp.raise_for_status()
    except (httpx.HTTPError, OSError):
        logger.warning("zitadel user search failed for org %s", org_id, exc_info=True)
        return {}

    users = {}
    for user in resp.json().get("result") or []:
        human = user.get("human") or {}
        users[user["id"]] = {
            "display_name": (human.get("profile") or {}).get("displayName")
            or user.get("preferredLoginName"),
            "email": (human.get("email") or {}).get("email"),
            "login_name": user.get("preferredLoginName"),
            "status": USER_STATE_TO_STATUS.get(user.get("state"), "unknown"),
        }
    return users


async def create_org(client: httpx.AsyncClient, name: str) -> str:
    """Create an org and return its id — the id a tenant row maps to.

    The caller of this API is the platform's own machine user, so it is
    the org's first ORG_OWNER; a human owner is added afterwards, and
    until then get_org_owner falls back to naming the machine user rather
    than claiming the org has none.

    Raises, like the other writes here: an org that may or may not exist
    is not something to guess about.
    """
    resp = await client.post(
        f"{ISSUER}/management/v1/orgs",
        headers=auth_header(),
        json={"name": name},
    )
    resp.raise_for_status()
    return resp.json()["id"]


async def delete_org(client: httpx.AsyncClient, org_id: str) -> None:
    """Remove an org. Only used to clean up one this process just made and
    could not finish provisioning."""
    resp = await client.delete(
        f"{ISSUER}/admin/v1/orgs/{org_id}", headers=auth_header()
    )
    resp.raise_for_status()


async def set_org_state(client: httpx.AsyncClient, org_id: str, active: bool) -> None:
    """Deactivate or reactivate one org — what suspending a tenant means.

    Unlike the reads above this one raises: a suspension that silently did
    nothing is worse than an error, because the operator would go on
    believing the tenant is locked out.
    """
    action = "_reactivate" if active else "_deactivate"
    resp = await client.post(
        f"{ISSUER}/management/v1/orgs/me/{action}",
        headers={**auth_header(), "x-zitadel-orgid": org_id},
        json={},
    )
    resp.raise_for_status()


async def get_org_owner(client: httpx.AsyncClient, org_id: str) -> dict | None:
    """The human ORG_OWNER of one org, as {user_id, display_name, login_name}.

    Scoped with x-zitadel-orgid, which is how the management API acts on an
    org other than the token's own. None when the org is gone (a tenant row
    outliving its Zitadel org answers 403 here), has no human owner, or
    Zitadel is down.
    """
    try:
        resp = await client.post(
            f"{ISSUER}/management/v1/orgs/me/members/_search",
            headers={**auth_header(), "x-zitadel-orgid": org_id},
            json={"query": {"limit": 100}},
        )
        resp.raise_for_status()
    except (httpx.HTTPError, OSError):
        logger.warning("zitadel owner lookup failed for org %s", org_id, exc_info=True)
        return None

    owners = [m for m in resp.json().get("result") or [] if "ORG_OWNER" in (m.get("roles") or [])]
    human = next((m for m in owners if m.get("userType") == _HUMAN), None)
    owner = human or next(iter(owners), None)
    if owner is None:
        return None
    return {
        "user_id": owner["userId"],
        "display_name": owner.get("displayName") or owner.get("preferredLoginName"),
        "login_name": owner.get("preferredLoginName"),
    }
