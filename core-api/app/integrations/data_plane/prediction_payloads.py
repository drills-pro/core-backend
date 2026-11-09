"""Prediction inputs and outputs too large for a `predictions` row (planning/24), kept in the staging bucket under
`predictions/<tenant>/<project>/<prediction>/`: the input at `input.json`, the output of attempt N at `<N>/output.json`, so a stale
attempt writes beside the current one, never over it. The row keeps the object key (`input_ref` / `output_ref`).

Every key is derived from the row's own tenant, project and id, and `load` refuses a key that is not under that prefix, so a forged or
corrupted ref cannot reach another tenant's object. S3 calls run in a thread (boto3 blocks)."""
import asyncio
import json
import uuid
from typing import Any

from botocore.exceptions import ClientError
from pydantic import BaseModel

from app.core import secret_files
from app.core.settings import settings
from app.integrations.data_plane import layout
from app.integrations.data_plane.lakefs import s3_client

_S3_NO_SUCH_KEY = "NoSuchKey"
_JSON_CONTENT_TYPE = "application/json"
_PARENT_SEGMENT = ".."


class PayloadRefError(ValueError):
    """A ref that is not under the prefix of the prediction it was read from."""


class PayloadMissing(RuntimeError):
    """The object a ref names is not in the bucket."""


class Payload(BaseModel):
    """Where a value went: `inline` (stays in the row's jsonb column) or `ref` (the object key, the column stays NULL)."""

    inline: Any = None
    ref: str | None = None


def _s3():
    return s3_client(secret_files.STAGING)


def _prefix(tenant_id: str, project_id: uuid.UUID | str, prediction_id: uuid.UUID | str) -> str:
    return layout.prediction_prefix(str(tenant_id), str(project_id), str(prediction_id))


def _put(key: str, body: bytes) -> None:
    _s3().put_object(Bucket=settings.data_plane.staging_bucket, Key=key, Body=body, ContentType=_JSON_CONTENT_TYPE)


async def store(tenant_id: str, project_id: uuid.UUID | str, prediction_id: uuid.UUID | str, name: str, value: Any, attempt: int | None = None) -> Payload:
    """`value` inline when its JSON fits predict.inline_max_bytes; otherwise written to `name` (under `<attempt>/` when given) and returned as a ref."""
    body = json.dumps(value).encode()
    if len(body) <= settings.predict.inline_max_bytes:
        return Payload(inline=value)
    key = _prefix(tenant_id, project_id, prediction_id) + (f"{attempt}/" if attempt is not None else "") + name
    await asyncio.to_thread(_put, key, body)
    return Payload(ref=key)


def _get(key: str) -> bytes:
    try:
        return _s3().get_object(Bucket=settings.data_plane.staging_bucket, Key=key)["Body"].read()
    except ClientError as e:
        if e.response["Error"]["Code"] == _S3_NO_SUCH_KEY:
            raise PayloadMissing(key) from e
        raise


async def load(tenant_id: str, project_id: uuid.UUID | str, prediction_id: uuid.UUID | str, ref: str) -> Any:
    """The value `ref` names, after checking it belongs to this tenant's, project's prediction."""
    if not ref.startswith(_prefix(tenant_id, project_id, prediction_id)) or _PARENT_SEGMENT in ref.split("/"):
        raise PayloadRefError(f"ref {ref!r} is not under the prefix of prediction {prediction_id}")
    return json.loads(await asyncio.to_thread(_get, ref))


def _delete_all(prefix: str) -> None:
    client = _s3()
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=settings.data_plane.staging_bucket, Prefix=prefix):
        keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        if keys:
            client.delete_objects(Bucket=settings.data_plane.staging_bucket, Delete={"Objects": keys})


async def delete(tenant_id: str, project_id: uuid.UUID | str, prediction_id: uuid.UUID | str) -> None:
    """Every object of the prediction (input, and the output of every attempt, stale ones too); nothing there is not an error."""
    await asyncio.to_thread(_delete_all, _prefix(tenant_id, project_id, prediction_id))
