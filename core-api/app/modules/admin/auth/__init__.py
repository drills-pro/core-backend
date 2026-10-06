"""The admin console's sign-in, run by core-api on the console's behalf:
Zitadel OIDC (./zitadel.py) or core-api's own password accounts
(./password.py)."""
from app.modules.admin.auth.routes import router

__all__ = ["router"]
