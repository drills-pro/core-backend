"""Self-serve signup (req/02 FR5). Login is Zitadel's own hosted OIDC flow, not ours."""
from app.modules.user.auth.signup import reap_stale_signups, router

__all__ = ["reap_stale_signups", "router"]
