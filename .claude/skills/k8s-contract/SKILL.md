---
name: k8s-contract
description: Keep core-backend and the k8s repo in sync. Use when a change touches config/prod.yaml, secret file names or keys, the Dockerfile, the port, health endpoints or probes, egress to other services, database names or roles, or the CI workflow that bumps the image.
---

# k8s contract

The interface is `docs/runtime-contract.md`. The `k8s` repo is a sibling directory (`../k8s`) and a push to its `main` deploys to production.

## When this applies

| Change here | Matching change in `../k8s` |
|---|---|
| New env knob only for prod | `config/prod.yaml` here, nothing there |
| New or renamed secret / key | seed it in `bootstrap/04-vault-seed-secrets.sh`; injector template picks it up |
| New dependency the backend calls | `apps/*/networkpolicy.yaml` allow rule |
| New port, probe path, hostname | `apps/backend/deployment.yaml`, `ingress.yaml` |
| New Postgres role or database | `apps/postgres/`, `apps/vault-roles/config/` (skill `postgres-setup` in that repo) |
| Image name or CI change | `.github/workflows/docker-publish.yml` calls `k8s/.github/workflows/bump-image.yml` |

## Rules

- Write the k8s change as a diff or description and let the user apply it; do not edit or push `../k8s` unprompted.
- Land the image and the manifest together when the image needs a new env var or secret (for example a new secret file); otherwise the pod fails to start. Say which order is safe.
- Validate on that side before pushing: `kubectl kustomize apps` builds cleanly (see `../k8s/CLAUDE.md`).
