# Zitadel + Vault integration pattern

How `core-api` authenticates to Zitadel and Vault, in production (k8s) and
local dev (`docker-compose`), and what a new service needs to replicate it.

## Two distinct Zitadel credentials — never merge them

| | Service User key (RFC 7523 JWT Profile) | OIDC login client |
|---|---|---|
| Code | `app/integrations/zitadel.py` | `app/modules/user/auth/routes.py::_zitadel_client_creds` |
| Shape | `{userId, keyId, key}` (RSA private key) | plain `client_id`/`client_secret` |
| Used for | Zitadel's own Management/Auth v2 APIs: `POST /v2/users/human` (register), `POST /v2/sessions` + `POST /v2/oidc/auth_requests/{id}` (login) | The login flow's own `authorization_code`+PKCE exchange: `GET /oauth/v2/authorize`, `POST /oauth/v2/token` |
| Identity | A **Service User** (Users → Service Users in Zitadel Cloud), granted an org role with user-management rights | A confidential **Web app** registered under a project |
| Vault path (prod) | `app-secret/app-backend/zitadel` | `app-secret/app-backend/zitadel-oidc` |
| Vault path (local dev) | n/a — falls back to a PAT file, see below | `app-secret/app-backend/zitadel-oidc` (same mount+path as prod) |

These are unrelated Zitadel resources granting unrelated capabilities. A
Service User key does not carry OIDC login-client permissions, and a Web
app's `client_secret` cannot call the Management API. Provisioning one does
not substitute for the other — a new service that needs both register/login
*and* its own OIDC login screen needs both credentials set up.

## Disk-based secrets in production, env-var/file fallbacks locally

Every credential is read from a **fixed disk path first**, falling back to
a plain env var or local-dev file when that path doesn't exist. This is
deliberate: in the cluster, a Vault Agent Injector sidecar renders these
paths and keeps them live (auto-renewed token, live-reloadable secrets)
without a static credential ever landing in a k8s `Secret` object or an env
var. Locally, `docker-compose` has no injector, so the fallback values keep
the stack working unmodified.

| What | Disk path (prod) | Local-dev fallback | Where |
|---|---|---|---|
| Vault's own auth token | `/vault/secrets/token` (k8s-auth, continuously renewed by the injector sidecar) | `VAULT_TOKEN` env var (`root` in compose) | `app/core/vault.py::_token()` |
| Zitadel Service User creds | `/var/app-secret/zitadel.env` (`ISSUER_URL`, `USER_ID`, `KEY_ID`) + `/var/app-secret/zitadel_key.pem` (RSA key) | `/pat/zitadel-automation.pat` (self-hosted Zitadel auto-provisions this on first init, see `scripts/zitadel-firstinstance.yaml`) — `get_pat()` tries the PAT file first and only builds+signs a JWT assertion if it's absent | `app/integrations/zitadel.py::get_pat()` |
| Zitadel OIDC login client | Vault KV `app-secret/app-backend/zitadel-oidc` via `vault.get_kv(..., mount="app-secret")` | same Vault path — `docker-compose`'s Vault dev-mode also gets an `app-secret` mount (`scripts/vault-setup.sh`), seeded by `scripts/zitadel-bootstrap.sh`/`zitadel-oidc-restore.sh` | `app/modules/user/auth/routes.py::_zitadel_client_creds()` |
| Any other KV read (`lakefs`, `ray`, etc.) | Vault mount named by `VAULT_KV_MOUNT` env var (`platform-secret` in k8s) | defaults to Vault's dev-mode default mount, `secret` | `app/core/vault.py::get_kv()` |

`app/core/config.py` is the single place that declares every disk path /
env var above — grep it before adding a new one.

## Dynamic Postgres credentials (Vault database secrets engine)

`core-api` never gets a static Postgres password for its own data-path
connection. `app/core/db.py` calls `vault.get_db_creds(role="core-app")`
(`app/core/vault.py`, wraps `secrets.database.generate_credentials`) at
startup and again on a timer (half the lease TTL), swapping the connection
pool each time. This needs, in Vault:

1. **`database/config/<name>`** — a connection config pointing at Postgres,
   authenticating as a dedicated admin role (not the app's own login role,
   not necessarily the Postgres superuser — see `bootstrap/03-vault-init.sh`
   in the k8s repo for the prod pattern: a role with just `CREATEROLE`).
2. **`database/roles/<role-name>`** — `creation_statements` that
   `CREATE ROLE "{{name}}" ... IN ROLE <group_role>`, `revocation_statements`
   that drop it, and a TTL.
3. A Postgres **`NOLOGIN` group role** (`core_app` for this app, see
   `core-api/migrations/0001_tenancy.sql`) holding the actual table grants —
   nothing ever connects as it directly; the dynamic per-lease roles inherit
   from it via `IN ROLE`.
4. The admin connection role needs `GRANT <group_role> TO <admin_role> WITH ADMIN OPTION`
   — Postgres refuses to `GRANT` a role you don't hold yourself.

Local dev's copy of this lives in `scripts/vault-setup.sh`.

## Onboarding a new service — checklist

1. **Postgres**: pick a database name, a dedicated admin role
   (`CREATEROLE`, not superuser), and a `NOLOGIN` group role for grants.
   Run the service's own migrations once against that database.
2. **Vault — database engine**: `vault write database/config/<name>` (admin
   role from step 1), `vault write database/roles/<name>` (creation/
   revocation statements `IN ROLE <group_role>`). See `bootstrap/03-vault-init.sh`
   in the k8s repo for the exact pattern this app uses (`core-postgres`
   connection, `core-app` role).
3. **Vault — app-secret policy + k8s auth role**: one Vault policy scoped to
   `app-secret/data/app-<service>/*` + `database/creds/<role>`, one
   `auth/kubernetes/role/app-<service>` bound to that service's own
   ServiceAccount. See `bootstrap/03-vault-init.sh`'s "Per-app secrets"
   section — add a new block there, don't widen an existing one.
4. **Vault — seed the actual secrets**: `bootstrap/04-vault-seed-secrets.sh`
   in the k8s repo is where real values (Zitadel creds, anything else)
   get `vault kv put` for each app. Add the new service's block there.
5. **Zitadel**, if the service needs its own users/login: create its own
   Service User (org-level, not a project app) for Management API access,
   grant it an org role, and — separately, only if it needs its own login
   screen — its own OIDC Web app. Never reuse another service's credentials
   of either kind.
6. **k8s Deployment**: Vault Agent Injector annotations rendering the
   service's own disk paths (mirror `apps/backend/deployment.yaml` in the
   k8s repo — `agent-inject-token` for the Vault token, per-secret
   `agent-inject-secret-*`/`agent-inject-file-*`/`secret-volume-path-*` for
   everything else).
7. **App code**: read every credential from its disk path first, with an
   env-var or local-file fallback for `docker-compose` — follow the table
   above as the template, and keep the fallback path working; that's what
   lets `docker compose up` still work without a Vault Agent Injector.
