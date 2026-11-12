"""The Hugging Face Hub, read only: what a base model contains, so a preset is written only for one the platform may
train from (docs 17, admission policy). One request per check: the model-info document at a pinned commit."""
from http import HTTPStatus

import httpx
from pydantic import BaseModel

from app.core.settings import settings

HUB_TIMEOUT_SECONDS = 10
SAFETENSORS_SUFFIX = ".safetensors"
AUTO_MAP_KEY = "auto_map"  # a config that names its own code classes needs trust_remote_code
LICENSE_TAG_PREFIX = "license:"


class HubUnavailable(Exception):
    """The Hub did not answer or does not know the repo at that commit."""


class ModelFacts(BaseModel):
    license: str | None
    safetensors_files: int
    total_bytes: int
    custom_code: bool


def facts_from(doc: dict) -> ModelFacts:
    files = doc.get("siblings") or []
    card = doc.get("cardData") or {}
    tag = next((t[len(LICENSE_TAG_PREFIX):] for t in doc.get("tags") or [] if t.startswith(LICENSE_TAG_PREFIX)), None)
    lic = card.get("license") or tag
    return ModelFacts(
        license=str(lic).lower() if lic else None,
        safetensors_files=sum(f.get("rfilename", "").endswith(SAFETENSORS_SUFFIX) for f in files),
        total_bytes=sum(f.get("size") or 0 for f in files),
        custom_code=AUTO_MAP_KEY in (doc.get("config") or {}),
    )


def refusals(facts: ModelFacts) -> list[str]:
    """Why the platform will not train from this model; empty = admitted."""
    out = []
    if not facts.safetensors_files:
        out.append("no .safetensors weights (pickled checkpoints are refused)")
    if facts.custom_code:
        out.append("its config needs custom code (auto_map)")
    if facts.license is None or facts.license not in settings.training.model_license_allowlist:
        out.append(f"licence {facts.license or 'not declared'} is not in the allowlist")
    if facts.total_bytes > settings.training.model_max_bytes:
        out.append(f"{facts.total_bytes} bytes is over the {settings.training.model_max_bytes} byte limit")
    return out


async def fetch(repo_id: str, revision: str) -> ModelFacts:
    url = f"{settings.training.hub_url}/api/models/{repo_id}/revision/{revision}"
    try:
        async with httpx.AsyncClient(timeout=HUB_TIMEOUT_SECONDS) as client:
            res = await client.get(url, params={"blobs": "true"})
    except httpx.HTTPError as e:
        raise HubUnavailable(str(e)) from e
    if res.status_code != HTTPStatus.OK:
        raise HubUnavailable(f"the Hub answered {res.status_code} for {repo_id}@{revision}")
    return facts_from(res.json())
