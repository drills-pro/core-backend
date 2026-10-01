"""The admin console's OIDC sign-in, run by core-api on the console's behalf."""
from app.modules.admin.auth.routes import router

__all__ = ["router"]
