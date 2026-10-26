"""drills.core_backend.auth.*: sign-in health. Login happens at Zitadel; this process sees each bearer token
it checks and each identity it meets for the first time."""
from app.core.metrics.base import counter

token_verifications = counter(
    "auth.token_verifications", "Bearer token checks by result (enums.AuthResult)"
)
principals_created = counter("auth.principals_created", "Zitadel identities seen for the first time (sign-ups)")
zitadel_userinfo = counter("auth.zitadel_userinfo", "Zitadel userinfo lookups by result (enums.UserinfoResult)")
