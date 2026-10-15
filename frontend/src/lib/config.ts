// Where core-api lives. In the compose stack it is published on :8090
// (docker-compose.yml maps 8090 -> the container's 8000), while the Vite
// dev server runs on :5173 — a real cross-origin call, which core-api's
// CORS middleware allows because the frontend authenticates with a Bearer
// header rather than cookies.
export const API_BASE = (import.meta.env.VITE_API_URL ?? 'http://localhost:8090').replace(/\/+$/, '')

// Dev escape hatch matching app/tenancy.py's own fallback: with no
// Authorization header, core-api accepts X-Tenant-Id. Only sent when no
// real token is stored, so a signed-in session always uses the JWT.
export const DEV_TENANT_ID: string | undefined = import.meta.env.VITE_DEV_TENANT_ID

// Zitadel hosted login (OIDC authorization code + PKCE, public client — no secret in the browser).
// The backend only verifies the resulting access token; it never sees a password.
export const ZITADEL_ISSUER = (import.meta.env.VITE_ZITADEL_ISSUER ?? '').replace(/\/+$/, '')
export const ZITADEL_CLIENT_ID: string = import.meta.env.VITE_ZITADEL_CLIENT_ID ?? ''
// Platform project id: requested as an audience so core-api accepts the token (app/core/auth.py).
export const ZITADEL_PROJECT_ID: string = import.meta.env.VITE_ZITADEL_PROJECT_ID ?? ''
