---
name: config-and-secrets
description: How core-api configuration and secrets work. Use whenever adding, changing or reading a setting, env var, credential, token, key, secret file path, or anything in core-api/config/*.yaml, app/core/settings.py or app/core/secret_files.py.
---

# Config and secrets

Non-secret config is committed YAML; secrets are mounted JSON files referenced by path. Contract: `docs/runtime-contract.md`.

## Add a setting

1. Add the field to the matching section class in `core-api/app/core/settings.py` (new section: add a class and a field on `Settings`).
2. Give it a value in `config/base.yaml`; override in `local.yaml`, `compose.yaml`, `prod.yaml` only where it differs. Every environment must still load: `tests/test_settings.py` checks all of them.
3. Read it as `settings.<section>.<field>`. Never `os.environ`.
4. If prod needs a different value it is a `prod.yaml` change; tell the user it ships with the next image.

## Add a secret

1. `SecretPaths` in `settings.py` + a path in `base.yaml` (cluster path, `/var/app-secret/app-backend/<name>`) and in `local.yaml`/`compose.yaml` (`.json` under `.local/secrets`).
2. Required keys in `secret_files.REQUIRED`.
3. Use `secret_files.get("<name>")` at the point of use, not at import, so rotation works.
4. Local value: `scripts/secrets-init.sh` (static) or the bootstrap script that generates it (`write_secret` in `scripts/lib-secrets.sh`).
5. Prod value: the `k8s` repo seeds it in Vault (`bootstrap/04-vault-seed-secrets.sh`, path `app-secret/app-backend/<name>`); the injector template already renders every key. Hand the user the `vault kv put` command; do not run root-token scripts.

## Rules

- A secret value never goes in YAML, env vars, logs, tests or this repo. `tests/test_settings.py` fails on secret-looking keys.
- core-api never calls Vault. Postgres credentials are the `db` secret file, rewritten by the Vault agent on rotation; `app/core/db.py` swaps the pool when it changes.
- `APP_ENV` has no default in code on purpose; the Dockerfile sets `prod`.
