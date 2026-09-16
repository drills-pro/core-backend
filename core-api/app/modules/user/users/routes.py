"""Profile settings and OAuth account linking (07-identity-oauth-linking.md).

FR5: username/display_name/avatar live on the platform's own `users` table,
keyed by Zitadel subject — email stays the actual identity anchor in
Zitadel itself, this table is display/vanity only.

FR1-FR3: linking Google/GitHub is Zitadel's own IdP-intent flow, not a
home-grown OAuth client — core-api just starts the intent (redirect to the
provider is expected, see FR2) and lets Zitadel complete the link on the
already-logged-in user.
"""
import httpx
from fastapi import APIRouter, Body, Depends, HTTPException

from app.core.db import get_pool
from app.core.tenancy import get_actor
from app.integrations import object_storage, zitadel

router = APIRouter(prefix="/v1/users/me", tags=["users"])

# ponytail: registered in scripts/zitadel-bootstrap.sh with placeholder
# client_id/secret — swap for real Google/GitHub OAuth app credentials
# when available, the linking flow below doesn't change.
IDP_NAME_BY_PROVIDER = {"google": "google", "github": "github"}


@router.get("")
async def get_profile(actor: str = Depends(get_actor)):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT zitadel_user_id, username, display_name, avatar_key FROM users WHERE zitadel_user_id = $1",
            actor,
        )
    if row is None:
        return {"zitadel_user_id": actor, "username": None, "display_name": None, "avatar_key": None}
    return dict(row)


@router.patch("")
async def update_profile(
    username: str | None = Body(default=None),
    display_name: str | None = Body(default=None),
    actor: str = Depends(get_actor),
):
    if username is None and display_name is None:
        raise HTTPException(status_code=400, detail="nothing to update")
    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                "INSERT INTO users (zitadel_user_id, username, display_name) VALUES ($1, $2, $3) "
                "ON CONFLICT (zitadel_user_id) DO UPDATE SET "
                "username = COALESCE(EXCLUDED.username, users.username), "
                "display_name = COALESCE(EXCLUDED.display_name, users.display_name), "
                "updated_at = now() "
                "RETURNING zitadel_user_id, username, display_name, avatar_key",
                actor,
                username,
                display_name,
            )
        except Exception as exc:  # noqa: BLE001 - unique violation on username
            if "unique" in str(exc).lower():
                raise HTTPException(status_code=409, detail="username already taken")
            raise
    return dict(row)


@router.post("/avatar-upload-url")
async def avatar_upload_url(filename: str, actor: str = Depends(get_actor)):
    key = f"avatars/{actor}/{filename}"
    url = object_storage.presigned_put_url(key, bucket=object_storage.PLATFORM_ASSETS_BUCKET)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (zitadel_user_id, avatar_key) VALUES ($1, $2) "
            "ON CONFLICT (zitadel_user_id) DO UPDATE SET avatar_key = EXCLUDED.avatar_key, updated_at = now()",
            actor,
            key,
        )
    return {"key": key, "url": url}


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
