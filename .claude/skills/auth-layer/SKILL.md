---
name: auth-layer
description: The authentication and authorization rework for core-api (principals, tenant_members, BFF session, authorize(), signup saga removal). Use when touching app/core/auth.py, tenancy.py, signup, invitations, /auth/* or /v1/me routes, roles, or Zitadel integration.
---

# Auth layer rework

Source of truth: `../planning/04-identity-tenancy-authz.md`, ADR-021/024/026 in `13-decisions.md`, tasks DRILLS-23/24/37/38/39 in `21` and `23`. Re-read them; they change.

## State of the code vs the plan

The code is still org-per-tenant: tenant resolved from the token's Zitadel org claim, roles from a Zitadel claim, the signup saga in `modules/user/auth/signup.py`, the SPA sending Bearer tokens. The plan: Zitadel only authenticates; tenants, members and roles are Postgres rows; core-api is the OIDC client with a cookie session (BFF).

## Order

1. Migration: `principals`, `tenant_members`, `platform_admins`, `sessions`, `realms`, `consents`; `tenants.zitadel_org_id` nullable.
2. Test harness: fake JWT issuer, two tenants, two principals (extends `tests/test_auth.py`).
3. `core/auth.py`: issuer allowlist, drop org and role claims.
4. `core/tenancy.py`: session or Bearer -> principal -> `X-Tenant-Id` membership check -> `SET LOCAL app.tenant_id`.
5. BFF endpoints (login, callback, logout, refresh, CSRF), `/v1/me`.
6. `POST /v1/tenants` in one transaction; then delete the saga, `signups`, the reaper and the provisioner user.
7. `core/authz.py`: closed action enum, one role table, `require(action, resource)`, plus the route x role matrix test.
8. Invitations accept flow; admin routes use `platform_admins` + MFA `amr`.

Steps 1-4 keep the Bearer path working; steps 5-6 break the current frontend and need a matching `customer-frontend` change.

## Rules

- Membership is checked in Postgres on every request; tokens carry identity only.
- Missing membership is 404, wrong role on a visible resource is 403.
- Settings and secret handling: skill `config-and-secrets`. New OIDC client secret = a new secret file, not an env var.
