"""Dataset preprocessing — Ray Data executor, Ludwig-shaped config
(improvement/phase2/04-labeling-preprocessing.md FR4-FR6).

Not Ludwig itself: preprocessing here produces a new *dataset* (a LakeFS
commit), it doesn't feed straight into training. Config is intentionally a
flat subset of Ludwig's own `preprocessing` block — four fixed operations
(resize, dedupe, class_balance, missing value handling is out of scope for
the image-classification-shaped datasets this targets first), not a DSL.
"""
import base64
import textwrap
import uuid

from fastapi import APIRouter, Body, Depends, HTTPException

from app.core import vault
from app.core.tenancy import get_tenant_id
from app.core.tenant_conn import TenantConn
from app.integrations import lakefs_client, ray_client

router = APIRouter(prefix="/v1/projects/{project_id}/datasets/{dataset_id}/preprocess", tags=["preprocessing"])

_VALID_CLASS_BALANCE = (None, "oversample", "undersample")

# FR5: hand-written Ray Data executor, not a DAG engine — read every
# labeled image off the dataset's LakeFS branch, apply the four fixed
# operations, write back as new files on the same branch (FR6: same-branch
# new commit, dataset identity stays stable).
_PREPROCESS_TEMPLATE = """
import hashlib
import io
import boto3
from botocore.client import Config
from collections import defaultdict
from PIL import Image

s3 = boto3.client("s3", endpoint_url={s3_endpoint!r},
    aws_access_key_id={s3_access_key!r}, aws_secret_access_key={s3_secret_key!r},
    config=Config(signature_version="s3v4"))

lakefs_repo, ref = {lakefs_repo!r}, {lakefs_ref!r}
keys = {keys!r}
resize = {resize!r}
dedupe = {dedupe!r}
class_balance = {class_balance!r}

by_label = defaultdict(list)
seen_hashes = set()
kept = 0
for key in keys:
    label = key.split("/", 1)[0]
    data = s3.get_object(Bucket=lakefs_repo, Key=f"{{ref}}/{{key}}")["Body"].read()
    if dedupe:
        h = hashlib.sha256(data).hexdigest()
        if h in seen_hashes:
            continue
        seen_hashes.add(h)
    if resize:
        img = Image.open(io.BytesIO(data)).convert("RGB").resize((resize["width"], resize["height"]))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        data = buf.getvalue()
    filename = key.rsplit("/", 1)[-1]
    by_label[label].append((filename, data))
    kept += 1

if class_balance and by_label:
    counts = {{label: len(files) for label, files in by_label.items()}}
    target = max(counts.values()) if class_balance == "oversample" else min(counts.values())
    for label, files in by_label.items():
        if class_balance == "oversample":
            i = 0
            while len(files) < target:
                files.append(files[i % len(files)])
                i += 1
        else:
            del files[target:]

written = 0
for label, files in by_label.items():
    for filename, data in files:
        s3.put_object(Bucket=lakefs_repo, Key=f"{{ref}}/preprocessed/{{label}}/{{filename}}", Body=data)
        written += 1

print(f"metric:preprocessed_files={{written}}")
"""


@router.post("", status_code=202)
async def preprocess_dataset(
    project_id: uuid.UUID,
    dataset_id: uuid.UUID,
    resize: dict | None = Body(default=None),
    dedupe: bool = Body(default=False),
    class_balance: str | None = Body(default=None),
    tenant_id: str = Depends(get_tenant_id),
):
    if class_balance not in _VALID_CLASS_BALANCE:
        raise HTTPException(status_code=400, detail=f"class_balance must be one of {_VALID_CLASS_BALANCE}")
    if resize is not None and not ({"width", "height"} <= resize.keys()):
        raise HTTPException(status_code=400, detail="resize must have 'width' and 'height'")

    async with TenantConn(tenant_id) as conn:
        dataset = await conn.fetchrow(
            "SELECT id FROM datasets WHERE id = $1 AND project_id = $2", dataset_id, project_id
        )
        if dataset is None:
            raise HTTPException(status_code=404, detail="dataset not found")

        ds_id = str(dataset_id)
        keys = [k for k in lakefs_client.list_files(ds_id) if not k.startswith("_unlabeled/") and "/" in k]
        if not keys:
            raise HTTPException(status_code=422, detail="dataset has no labeled files to preprocess")

        config = {"resize": resize, "dedupe": dedupe, "class_balance": class_balance}
        lakefs_creds = vault.get_kv("lakefs")
        script = textwrap.dedent(_PREPROCESS_TEMPLATE).format(
            lakefs_repo=lakefs_client.REPO,
            lakefs_ref=ds_id,
            keys=keys,
            resize=resize,
            dedupe=dedupe,
            class_balance=class_balance,
            s3_endpoint=lakefs_creds["endpoint"],
            s3_access_key=lakefs_creds["access_key"],
            s3_secret_key=lakefs_creds["secret_key"],
        )
        encoded = base64.b64encode(script.encode()).decode()
        entrypoint = f'python -c "import base64; exec(base64.b64decode(\'{encoded}\'))"'
        ray_job_id = await ray_client.submit_raw_job(
            entrypoint=entrypoint,
            metadata={"tenant_id": tenant_id, "project_id": str(project_id), "job_type": "preprocess"},
        )
        row = await conn.fetchrow(
            "INSERT INTO dataset_preprocessing_jobs (tenant_id, project_id, dataset_id, ray_job_id, config) "
            "VALUES ($1, $2, $3, $4, $5) RETURNING id, status, config, created_at",
            tenant_id,
            project_id,
            dataset_id,
            ray_job_id,
            config,
        )
    return dict(row)


@router.get("")
async def list_preprocessing_jobs(
    project_id: uuid.UUID, dataset_id: uuid.UUID, tenant_id: str = Depends(get_tenant_id)
):
    async with TenantConn(tenant_id) as conn:
        rows = await conn.fetch(
            "SELECT id, status, config, output_ref, error, created_at, completed_at "
            "FROM dataset_preprocessing_jobs WHERE dataset_id = $1 AND project_id = $2 ORDER BY created_at DESC",
            dataset_id,
            project_id,
        )
    return [dict(r) for r in rows]


