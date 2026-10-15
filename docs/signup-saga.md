# Self-serve signup: org + tenant saga

`POST /v1/auth/signup {org_name, first_name, last_name, email, password}` (unauthenticated,
rate-limited) replaces `/v1/auth/register`, which put every user in the service user's own org.
Same saga is what `POST /v1/admin/tenants` should run (minus the owner), so there is one
provisioning path.

Runs as the `provisioner` service user (instance role `IAM_ORG_MANAGER`, plus membership of the
org that owns the platform project so it can create project grants). Never as the login client.

## Steps

| # | Action (Zitadel unless noted) | Compensation |
|---|---|---|
| 1 | `POST /v2/organizations` with `admins:[{human:{profile,email(isVerified:false),password}, roles:["ORG_OWNER"]}]` — creates org **and** its first user in one call | `DELETE /admin/v1/orgs/{org}` (removes the user and any grants) |
| 2 | `POST /management/v1/projects/{PROJECT}/grants` `{grantedOrgId: org, roleKeys:[org_owner, org_admin, project_editor, project_viewer]}` | covered by step 1's delete |
| 3 | `POST /management/v1/users/{owner}/grants` (header `x-zitadel-orgid: org`) `{projectId, projectGrantId, roleKeys:[org_owner]}` | covered by step 1's delete |
| 4 | Postgres: insert `tenants` row + `Default` team (existing `_insert_tenant`) | delete org (step 1) |

Zitadel first, Postgres last, same ordering rationale as `create_tenant`: a tenant row pointing at
no org is the worse half-state. The tenant only becomes usable at step 4, so a login that races
the saga gets 403 "organization not provisioned", which is already the correct answer.

Failure at any step → run the compensation, return 502 (Zitadel) / 409 (duplicate org name or
email). Compensation failing is logged as an orphan, never masks the original error.

## Idempotency and orphans

- Client sends an `Idempotency-Key`; a `signups(key, email, org_id, state, created_at)` row
  (state: `started|org|granted|done`) lets a retry resume at the failed step instead of creating
  a second org.
- Reaper (existing `background.py` pattern): orgs in Zitadel with no `tenants` row and a
  `signups` row older than 15 min in a non-`done` state are deleted.

## Abuse and trust

- The email is **not** marked verified (the old `isVerified: True` let anyone claim any
  address). Zitadel sends the verification code; `get_tenant_id` should additionally require the
  `email_verified` claim before step 4's tenant is usable, or the saga defers step 4 until verified.
- Rate limit per IP and per email (reuse `core/rate_limit.py`), plus a CAPTCHA at the edge.
- Org names are free text → the tenant `name` stays display-only; identity is `zitadel_org_id`.

## Prerequisites outside this repo (k8s / Zitadel Cloud)

- Seed `project_id` into `app-secret/app-backend/zitadel-oidc` (or set `ZITADEL_PROJECT_ID`), now
  required by token verification.
- Split the service user: `provisioner` (`IAM_ORG_MANAGER`) and, only if the embedded login stays,
  `login-client` (`IAM_LOGIN_CLIENT`).
- Project: role assertion on, access token type JWT, roles above defined.
