"""The admin console's sign-in, run by core-api on the console's behalf:
Zitadel OIDC (./zitadel.py) or core-api's own password accounts
(./password.py) — and, for the latter, the signed-in operator's own
profile, preferences and password (./account.py)."""
from app.modules.admin.auth.account import router as account_router
from app.modules.admin.auth.routes import router

__all__ = ["account_router", "router"]
