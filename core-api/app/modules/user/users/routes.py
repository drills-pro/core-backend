"""OAuth account linking (07-identity-oauth-linking.md).

Profile data (name, email) lives only in Zitadel and reaches the frontend as token claims;
core-api keeps no users table (migration 0035).

FR1-FR3: linking Google/GitHub is Zitadel's own IdP-intent flow, not a
home-grown OAuth client — core-api just starts the intent (redirect to the
provider is expected, see FR2) and lets Zitadel complete the link on the
already-logged-in user.
"""
import httpx
from fastapi import APIRouter, Depends, HTTPException

from app.core.tenancy import get_actor
from app.integrations import zitadel

router = APIRouter(prefix="/v1/users/me", tags=["users"])

# ponytail: registered in scripts/zitadel-bootstrap.sh with placeholder
# client_id/secret — swap for real Google/GitHub OAuth app credentials
# when available, the linking flow below doesn't change.
IDP_NAME_BY_PROVIDER = {"google": "google", "github": "github"}


@router.get("/idp-intent")
async def start_idp_intent(provider: str, return_url: str):
    """FR3: user is already logged in; this just gets the URL to redirect
    the browser to Google/GitHub for consent. Zitadel completes the link
    on the current user's account when the callback lands — core-api is
    not in that callback path at all, Zitadel's own configured redirect is."""
    if provider not in IDP_NAME_BY_PROVIDER:
        raise HTTPException(status_code=400, detail=f"provider must be one of {list(IDP_NAME_BY_PROVIDER)}")
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{zitadel.ISSUER}/v2/idp_intents",
            headers=zitadel.auth_header(),
            json={"idpId": IDP_NAME_BY_PROVIDER[provider], "urls": {"successUrl": return_url, "failureUrl": return_url}},
        )
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"could not start idp intent: {resp.text}")
    return resp.json()


@router.get("/idps")
async def list_linked_idps(actor: str = Depends(get_actor)):
    # Zitadel is the source of truth for which login methods a user has —
    # this reflects it rather than tracking a shadow copy in core-api's DB.
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{zitadel.ISSUER}/v2/users/{actor}/idps",
            headers=zitadel.auth_header(),
        )
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"could not list idps: {resp.text}")
    return resp.json()


@router.delete("/idps/{link_id}", status_code=204)
async def unlink_idp(link_id: str, actor: str = Depends(get_actor)):
    # FR4: Zitadel itself refuses to remove the last usable login method,
    # so no extra "is this the last one" check is needed here.
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.delete(
            f"{zitadel.ISSUER}/v2/users/{actor}/idps/{link_id}",
            headers=zitadel.auth_header(),
        )
    if resp.status_code >= 400:
        raise HTTPException(status_code=400, detail=f"could not unlink: {resp.text}")
