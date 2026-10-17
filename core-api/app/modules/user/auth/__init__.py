"""/v1/me and tenant creation. Login is Zitadel's own hosted OIDC flow, not ours."""
from app.modules.user.auth.routes import router

__all__ = ["router"]
