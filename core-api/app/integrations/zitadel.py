"""Zitadel management-API access shared by the modules that talk to it.

The Personal Access Token is mounted by docker-compose from the
`zitadel-pat` volume (see scripts/zitadel-firstinstance.yaml) and read
once, lazily — it does not exist yet at import time on a cold stack.

Both the embedded login flow (app/modules/auth) and profile/IdP linking
(app/modules/users) authenticate as this PAT; before the modular split
the latter reached into the former's private `_get_pat`, which is what
this module replaces.
"""
import logging

import httpx

from app.core.config import ZITADEL_LOGIN_ISSUER

logger = logging.getLogger(__name__)

ISSUER = ZITADEL_LOGIN_ISSUER
PAT_PATH = "/pat/zitadel-automation.pat"

_pat_cache: str | None = None


def get_pat() -> str:
    global _pat_cache
    if _pat_cache is None:
        with open(PAT_PATH) as f:
            _pat_cache = f.read().strip()
    return _pat_cache


def auth_header() -> dict[str, str]:
    return {"Authorization": f"Bearer {get_pat()}"}


# --- Org-level reads for the back-office (app/modules/admin) ---------------
# Zitadel owns org state and org membership; core-api's `tenants` table only
# holds the mapping to them (0001: "org maps 1:1 to tenant"). Rather than
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
