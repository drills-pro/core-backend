---
name: auth-layer
description: The authentication and authorization rework for core-api (principals, tenant_members, BFF session, authorize(), admin without Zitadel orgs). Use when touching app/core/auth.py, tenancy.py, tenant creation, invitations, /auth/* or /v1/me routes, roles, or Zitadel integration.
---

# Auth layer rework

Source of truth: `../planning/04-identity-tenancy-authz.md`, ADR-021/024/026 in `13-decisions.md`, tasks DRILLS-23/24/37/38/39 in `21` and `23`. Re-read them; they change.

## State of the code vs the plan

Done (shared pool, verify-only; migration `20261018090000_shared_identity_pool.sql`): `core/auth.py` verifies
signature, issuer and audience and gets identity (`email`, `email_verified`, `name`) from the token or, when the access token lacks
them (the usual case), from Zitadel's userinfo via `get_profile_claims`, used only by `/v1/me` and tenant creation; `core/tenancy.py` resolves the tenant from
`tenant_members` on every request (`X-Tenant-Id`, optional when the user has exactly one tenant; no
membership is 404); `GET /v1/me` and `POST /v1/tenants` live in `modules/user/auth/routes.py`; the signup
saga, `signups`, the reaper and the `zitadel_oidc` secret are gone. `tenants.zitadel_org_id` is nullable and
only legacy tenants and the admin back-office still use it (admin code treats NULL as "no org").

Still to do, in order:

1. Test harness with two tenants and two principals (today `tests/test_auth.py` fakes the pool).
2. Issuer allowlist and `realms`; `platform_admins` (+ MFA `amr`) replacing the `platform_admin` Zitadel role claim that `get_platform_admin` still reads; then delete the provisioner service user and the org helpers the admin routes use.
3. BFF endpoints (login, callback, logout, refresh, CSRF) and `sessions`; this is the only step that needs an OIDC client secret, as a new secret file.
4. `core/authz.py`: closed action enum, one role table, `require(action, resource)`, plus the route x role matrix test. Roles gate nothing yet.
5. Invitations accept flow with the verified-email check; `consents` and Turnstile on tenant creation.

`POST /v1/tenants` replaced `POST /v1/auth/signup`, so `customer-frontend` must call it (and `/v1/me`) instead.

## Rules

- Membership is checked in Postgres on every request; tokens carry identity only.
- Missing membership is 404, wrong role on a visible resource is 403.
- Settings and secret handling: skill `config-and-secrets`. A future OIDC client secret = a new secret file, not an env var. Locally the audience comes from `CORE_ZITADEL__PROJECT_ID` (printed by `scripts/steps/zitadel-restore.sh`); prod sets `zitadel.project_id` in `prod.yaml`.
