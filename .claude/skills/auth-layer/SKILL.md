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

Also done (2026-10-21): `core/authz/` (closed `Action` enum, one role table, `require(action)`, route x role matrix
test); `tenant_members` module (members, invitations, ownership offers, leave) and `tenants` module (read, rename,
soft delete, restore); `/v1/me` capabilities; idempotent `POST /v1/tenants`. Rules and routes: planning 04 and 05.

Still to do, in order:

1. Test harness with two tenants and two principals (today `tests/core/authz` fakes the pool).
2. Issuer allowlist and `realms`; `platform_admins` (+ MFA `amr`) replacing the `platform_admin` Zitadel role claim that `get_platform_admin` still reads; then delete the provisioner service user and the org helpers the admin routes use.
3. `consents` ledger and Turnstile on tenant creation (Turnstile needs a secret file and a `k8s` change); the 30-day tenant purge after soft delete.
4. BFF endpoints (login, callback, logout, refresh, CSRF) and `sessions`: **out of scope for now**; frontends use the bearer token.

`POST /v1/tenants` replaced `POST /v1/auth/signup`, so `customer-frontend` must call it (and `/v1/me`) instead.

## Rules

- Membership is checked in Postgres on every request; tokens carry identity only.
- Missing membership is 404, wrong role on a visible resource is 403.
- Settings and secret handling: skill `config-and-secrets`. A future OIDC client secret = a new secret file, not an env var. Locally the audience comes from `CORE_ZITADEL__PROJECT_ID` (printed by `scripts/steps/zitadel-restore.sh`); prod sets `zitadel.project_id` in `prod.yaml`.
