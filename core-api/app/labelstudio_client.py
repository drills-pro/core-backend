"""Label Studio API client (req/04: "Labeling | proxy/webhook to Label
Studio project, pull labeled export"; req/05's labeling UX backend).

Label Studio's newer versions disabled legacy static API tokens
org-wide by default — auth is short-lived JWT access tokens exchanged
from a long-lived refresh token. The refresh token (obtained once via
the admin session login, see README) lives in Vault; this module handles
the access-token exchange/caching so callers never see the JWT dance.
"""
import os
import time

import httpx

from app import vault

# Same host-vs-container split as object_storage.py's MINIO_PUBLIC_ENDPOINT/
# lakefs_client.py's PUBLIC_ENDPOINT — vault's "url" is the internal
# docker-network address core-api uses server-to-server; the browser
# embedding Label Studio in an iframe needs an address it can resolve.
# OrbStack's <service>.<project>.orb.local DNS (already used for Zitadel,
# see docker-compose.yml) resolves from the host too and isn't subject to
# the localhost:<port> forward's occasional connection resets.
PUBLIC_ENDPOINT = os.environ.get("LABELSTUDIO_PUBLIC_ENDPOINT", "http://labelstudio.monorepo.orb.local:8080")

_access_token: str | None = None
_access_token_expires_at: float = 0


async def _get_access_token() -> str:
    global _access_token, _access_token_expires_at
    if _access_token and time.time() < _access_token_expires_at:
        return _access_token

    creds = vault.get_kv("platform/labelstudio")
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{creds['url']}/api/token/refresh/", json={"refresh": creds["refresh_token"]}
        )
        resp.raise_for_status()
    _access_token = resp.json()["access"]
    _access_token_expires_at = time.time() + 240  # refresh a bit before the ~300s expiry
    return _access_token


async def _client() -> httpx.AsyncClient:
    creds = vault.get_kv("platform/labelstudio")
    token = await _get_access_token()
    return httpx.AsyncClient(base_url=creds["url"], headers={"Authorization": f"Bearer {token}"}, timeout=10)


async def create_project(*, title: str, label_config: str) -> int:
    async with await _client() as client:
        resp = await client.post("/api/projects/", json={"title": title, "label_config": label_config})
        resp.raise_for_status()
        return resp.json()["id"]


async def import_tasks(project_id: int, tasks: list[dict]) -> int:
    # FR1 (04-labeling-preprocessing.md): the task source is presigned-URL-
    # readable LakeFS objects — Label Studio just needs a URL per task, it
    # never touches our storage credentials. Each task also carries its own
    # LakeFS key (echoed back unchanged in Label Studio's export under
    # task.data.key) so FR2's import-back doesn't have to reverse-parse a
    # presigned URL to know which object a label belongs to.
    async with await _client() as client:
        resp = await client.post(f"/api/projects/{project_id}/import", json=tasks)
        resp.raise_for_status()
        return resp.json().get("task_count", len(tasks))


async def get_project_stats(project_id: int) -> dict:
    async with await _client() as client:
        resp = await client.get(f"/api/projects/{project_id}/")
        resp.raise_for_status()
        data = resp.json()
        return {
            "task_number": data.get("task_number", 0),
            "finished_task_number": data.get("finished_task_number", 0),
        }


async def export_annotations(project_id: int) -> list:
    """FR: "pull labeled export"."""
    async with await _client() as client:
        resp = await client.get(f"/api/projects/{project_id}/export", params={"exportType": "JSON"})
        resp.raise_for_status()
        return resp.json()
