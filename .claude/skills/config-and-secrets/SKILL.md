---
name: config-and-secrets
description: How core-api configuration and secrets work. Use whenever adding, changing or reading a setting, env var, credential, token, key, secret file path, or anything in core-api/config/*.yaml, app/core/settings.py or app/core/secret_files.py.
---

# Config and secrets

Non-secret config is committed YAML; secrets are mounted JSON files referenced by path. Contract: `docs/runtime-contract.md`.

## Add a setting

1. Add the field to the matching section class in `core-api/app/core/settings.py` (new section: add a class and a field on `Settings`).
2. Give it a value in `config/base.yaml`; override in `local.yaml`, `compose.yaml`, `prod.yaml` only where it differs. Every environment must still load: `tests/core/test_settings.py` checks all of them.
   A service URL differs per environment: compose service name in `compose.yaml` (`http://lakefs:8000`), `127.0.0.1`
   and the published port in `local.yaml` (not `localhost`: it tries `::1` first, which OrbStack resets for
   IPv4-only containers; publish the port in `docker-compose.yml` if it is not), cluster DNS in `prod.yaml`.
3. Read it as `settings.<section>.<field>`. Never `os.environ`.
4. If prod needs a different value it is a `prod.yaml` change; tell the user it ships with the next image.

## Add a secret

1. `SecretPaths` in `settings.py` + a path in `base.yaml` (cluster path, `/var/app-secret/app-backend/<name>`), in
   `compose.yaml` (`/var/app-secret/app-backend/<name>.json`, where compose mounts `.local/secrets/`) and in `local.yaml`
   (`../.local/secrets-host/<name>.json`).
2. Required keys in `secret_files.REQUIRED`.
3. Use `secret_files.get("<name>")` at the point of use, not at import, so rotation works.
4. Local value: `scripts/secrets-init.sh` (static) or the bootstrap step that generates it (`write_secret` in
   `scripts/lib/secrets.sh`), always into `.local/secrets/`. `bootstrap.sh` copies every file to `.local/secrets-host/`
   for core-api run natively, rewriting `http://<service>:<port>` to OrbStack's `<service>.<project>.orb.local`. So a
   URL inside a secret uses the compose service name in that form, and both copies work.
5. Prod value: the `k8s` repo seeds it in Vault (`bootstrap/04-vault-seed-secrets.sh`, path `app-secret/app-backend/<name>`); the injector template already renders every key. Hand the user the `vault kv put` command; do not run root-token scripts.

## Datasets knobs and secrets

`data_plane:` (buckets, presign TTL) and `dataset_limits:` (upload, archive, image, CSV, HF, job and per-tenant
import caps) live in `base.yaml`; add a limit there and in `settings.py`, never as a literal in the ingest code.
The secrets are `lakefs`, `staging` (core-api's S3 access to the staging and lakeFS blockstore buckets) and `ingest`
(handed to the Ray ingest job; staging bucket only). Only `app/integrations/data_plane/` reads them. New `prod.yaml`
keys or secret files need the `k8s` change listed in `docs/runtime-contract.md` (skill `k8s-contract`).

## Rules

- A secret value never goes in YAML, env vars, logs, tests or this repo. `tests/core/test_settings.py` fails on secret-looking keys.
- core-api never calls Vault. Postgres credentials are the `db` secret file, rewritten by the Vault agent on rotation; `app/core/db.py` swaps the pool when it changes.
- Migrations have their own credential, not core-api's: the k8s Job renders `/vault/secrets/dbmate.env`
  (`DATABASE_URL` for `backend_admin`, Vault `database/static-creds/backend-admin`). core-api has no setting
  for it. Locally the compose `dbmate` service logs in as `backend_admin` (`compose/postgres/core-api.sql`).
- `APP_ENV` has no default in code on purpose; the Dockerfile sets `prod`.
