"""Zitadel management-API access shared by the modules that talk to it.

The Personal Access Token is mounted by docker-compose from the
`zitadel-pat` volume (see scripts/zitadel-firstinstance.yaml) and read
once, lazily — it does not exist yet at import time on a cold stack.

Both the embedded login flow (app/modules/auth) and profile/IdP linking
(app/modules/users) authenticate as this PAT; before the modular split
the latter reached into the former's private `_get_pat`, which is what
this module replaces.
"""
from app.core.config import ZITADEL_LOGIN_ISSUER

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
