import csv
import io
import json
import logging
import uuid
import zipfile

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, UploadFile

from app import lakefs_client, ray_client
from app.db import get_pool
from app.tenancy import get_tenant_id
from app.tenant_conn import TenantConn

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/projects/{project_id}/datasets", tags=["datasets"])
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".webp")


async def _get_dataset(conn, dataset_id: uuid.UUID, project_id: uuid.UUID):
    row = await conn.fetchrow(
        "SELECT id, s3_prefix FROM datasets WHERE id = $1 AND project_id = $2", dataset_id, project_id
    )
    if row is None:
        raise HTTPException(status_code=404, detail="dataset not found")
    return row


# FR1 (03-dataset-governance.md): which import shapes each task_type accepts.
# 'legacy_untyped' (02 FR6) bypasses the gate entirely — pre-Phase-2 projects
# keep working unchanged.
_SHAPE_GATE = {
    "image_classification": {"upload-url", "upload-zip", "import-hf-images"},
    "object_detection": {"upload-yolo", "upload-coco"},
    "tabular_classification": {"tabular", "import-hf-tabular"},
    "text_classification": {"import-hf-tabular"},
}


async def _check_task_type_gate(conn, project_id: uuid.UUID, shape: str) -> None:
    task_type = await conn.fetchval("SELECT task_type FROM projects WHERE id = $1", project_id)
    if task_type is None or task_type == "legacy_untyped":
        return
    allowed = _SHAPE_GATE.get(task_type, set())
    if shape not in allowed:
        raise HTTPException(
            status_code=422,
            detail=f"project task_type '{task_type}' does not accept '{shape}' uploads",
        )


@router.post("", status_code=201)
async def create_dataset(project_id: uuid.UUID, name: str, tenant_id: str = Depends(get_tenant_id)):
    async with TenantConn(tenant_id) as conn:
        row = await conn.fetchrow(
            "INSERT INTO datasets (tenant_id, project_id, name, s3_prefix) "
            "VALUES ($1, $2, $3, '') RETURNING id",
            tenant_id,
            project_id,
            name,
        )
        # req/01: per-tenant object storage prefix — informational only
        # now (files live under a LakeFS branch named for the dataset's
        # own id, see app/lakefs_client.py), kept for display/back-compat.
        s3_prefix = f"tenants/{tenant_id}/projects/{project_id}/datasets/{row['id']}"
        row = await conn.fetchrow(
            "UPDATE datasets SET s3_prefix = $2 WHERE id = $1 RETURNING id, name, s3_prefix, created_at",
            row["id"],
            s3_prefix,
        )
    lakefs_client.ensure_branch(str(row["id"]))
    return dict(row)


@router.get("")
async def list_datasets(
    project_id: uuid.UUID,
    tenant_id: str = Depends(get_tenant_id),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    async with TenantConn(tenant_id) as conn:
        rows = await conn.fetch(
            "SELECT id, name, s3_prefix, created_at FROM datasets WHERE project_id = $1 "
            "ORDER BY created_at DESC LIMIT $2 OFFSET $3",
            project_id,
            limit,
            offset,
        )
    return [dict(r) for r in rows]


@router.post("/{dataset_id}/upload-url")
async def get_upload_url(
    project_id: uuid.UUID,
    dataset_id: uuid.UUID,
    filename: str,
    label: str,
    tenant_id: str = Depends(get_tenant_id),
):
    """FR (04-api.md): "upload returns presigned S3/MinIO URL, client PUTs
    directly — don't proxy large files through the app backend."
    `label` is the YOLO-classify-style subfolder (class name) the file
    belongs under — matches the directory convention app/ray_client.py's
    Ultralytics backend trains on directly."""
    async with TenantConn(tenant_id) as conn:
        await _get_dataset(conn, dataset_id, project_id)
        await _check_task_type_gate(conn, project_id, "upload-url")
    key = f"{label}/{filename}"
    return {"key": key, "url": lakefs_client.presigned_put_url(str(dataset_id), key)}


@router.post("/{dataset_id}/unlabeled-upload-url")
async def get_unlabeled_upload_url(
    project_id: uuid.UUID, dataset_id: uuid.UUID, filename: str, tenant_id: str = Depends(get_tenant_id)
):
    """In-app labeling canvas (req/05): upload a raw file with no class
    yet — it lands in `_unlabeled/` until someone labels it via
    POST .../label below."""
    async with TenantConn(tenant_id) as conn:
        await _get_dataset(conn, dataset_id, project_id)
    key = f"_unlabeled/{filename}"
    return {"key": key, "url": lakefs_client.presigned_put_url(str(dataset_id), key)}


@router.get("/{dataset_id}/unlabeled")
async def list_unlabeled(project_id: uuid.UUID, dataset_id: uuid.UUID, tenant_id: str = Depends(get_tenant_id)):
    async with TenantConn(tenant_id) as conn:
        await _get_dataset(conn, dataset_id, project_id)
    keys = lakefs_client.list_files(str(dataset_id), prefix="_unlabeled/")
    return [{"key": k, "view_url": lakefs_client.presigned_get_url(str(dataset_id), k)} for k in keys]


@router.post("/{dataset_id}/label")
async def label_file(
    project_id: uuid.UUID, dataset_id: uuid.UUID, key: str, label: str, tenant_id: str = Depends(get_tenant_id)
):
    """The actual labeling action: moves an unlabeled upload into its
    class folder (the same convention app/ray_client.py's Ultralytics
    backend already trains on) and appends a manifest row — this is a
    real, minimal in-app labeling loop, distinct from the full Label
    Studio integration (app/labeling.py) for teams that want something
    lighter than a separate labeling tool."""
    async with TenantConn(tenant_id) as conn:
        await _get_dataset(conn, dataset_id, project_id)
    if not key.startswith("_unlabeled/"):
        raise HTTPException(status_code=400, detail="key is not an unlabeled file in this dataset")

    ds_id = str(dataset_id)
    filename = key.rsplit("/", 1)[-1]
    dst_key = f"{label}/{filename}"
    lakefs_client.copy(ds_id, key, dst_key)
    lakefs_client.delete(ds_id, key)

    try:
        existing = lakefs_client.download(ds_id, "manifest.csv").decode()
        rows = list(csv.DictReader(io.StringIO(existing)))
    except Exception:  # noqa: BLE001 - no manifest yet
        rows = []
    rows.append({"image_id": filename, "image_path": dst_key, "class_no": label})

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=["image_id", "image_path", "class_no"])
    writer.writeheader()
    writer.writerows(rows)
    lakefs_client.upload(ds_id, "manifest.csv", buf.getvalue().encode())

    return {"key": dst_key, "label": label, "manifest_rows": len(rows)}


@router.post("/{dataset_id}/manifest")
async def upload_manifest(
    project_id: uuid.UUID, dataset_id: uuid.UUID, request: Request, tenant_id: str = Depends(get_tenant_id)
):
    """req/06 dataset-ingestion format: "CSV/manifest with image_id,
    image_path,class_no (or bbox/mask equivalents)". image_path values
    are expected to be keys already uploaded under this dataset (via
    upload-url) — the manifest just says which uploaded file is which
    example and label, it doesn't itself carry image bytes."""
    async with TenantConn(tenant_id) as conn:
        await _get_dataset(conn, dataset_id, project_id)

    raw = await request.body()
    reader = csv.DictReader(io.StringIO(raw.decode()))
    required = {"image_id", "image_path", "class_no"}
    if not required.issubset(reader.fieldnames or []):
        raise HTTPException(status_code=400, detail=f"manifest CSV must have columns: {sorted(required)}")
    rows = list(reader)

    lakefs_client.upload(str(dataset_id), "manifest.csv", raw)
    return {"rows": len(rows), "classes": sorted({r["class_no"] for r in rows})}


@router.get("/{dataset_id}/manifest")
async def get_manifest(
    project_id: uuid.UUID, dataset_id: uuid.UUID, tenant_id: str = Depends(get_tenant_id)
):
    async with TenantConn(tenant_id) as conn:
        await _get_dataset(conn, dataset_id, project_id)
    try:
        raw = lakefs_client.download(str(dataset_id), "manifest.csv")
    except Exception:  # noqa: BLE001 - no manifest uploaded yet
        raise HTTPException(status_code=404, detail="no manifest uploaded for this dataset")
    return list(csv.DictReader(io.StringIO(raw.decode())))


@router.post("/{dataset_id}/upload-zip")
async def upload_zip(
    project_id: uuid.UUID, dataset_id: uuid.UUID, file: UploadFile, tenant_id: str = Depends(get_tenant_id)
):
    """Bulk import: a .zip of `<label>/<file>` folders (the same
    class-label layout the Ultralytics backend trains on directly, and
    what `upload-url` builds one file at a time). Unlike upload-url, this
    genuinely goes through core-api rather than direct-to-S3 — it has to,
    since the server is the one unpacking the archive."""
    async with TenantConn(tenant_id) as conn:
        await _get_dataset(conn, dataset_id, project_id)
        await _check_task_type_gate(conn, project_id, "upload-zip")

    data = await file.read()
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="not a valid zip file")

    ds_id = str(dataset_id)
    uploaded, skipped, by_label = 0, 0, {}
    for name in zf.namelist():
        if name.endswith("/") or "/" not in name.strip("/") or not name.lower().endswith(_IMAGE_EXTS):
            skipped += 1
            continue
        # first path segment is the class label, mirroring upload-url's convention
        parts = name.strip("/").split("/")
        label, filename = parts[0], parts[-1]
        if not label or not filename or label.startswith("__MACOSX"):
            skipped += 1
            continue
        lakefs_client.upload(ds_id, f"{label}/{filename}", zf.read(name))
        by_label[label] = by_label.get(label, 0) + 1
        uploaded += 1

    return {"uploaded": uploaded, "skipped": skipped, "labels": by_label}


@router.post("/{dataset_id}/upload-yolo")
async def upload_yolo(
    project_id: uuid.UUID, dataset_id: uuid.UUID, file: UploadFile, tenant_id: str = Depends(get_tenant_id)
):
    """FR2 (03-dataset-governance.md): a .zip in YOLO layout —
    `images/<file>` + `labels/<file>.txt` (one bounding box per line:
    `class_id x_center y_center width height`, normalized 0-1). Stored
    under the same paths in LakeFS, plus `classes.txt` if the zip has one
    (`obj.names`/`classes.txt`), unlike upload-zip's folder-per-class
    convention this is for object_detection projects with real ground-truth
    boxes, not just class labels."""
    async with TenantConn(tenant_id) as conn:
        await _get_dataset(conn, dataset_id, project_id)
        await _check_task_type_gate(conn, project_id, "upload-yolo")

    data = await file.read()
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="not a valid zip file")

    ds_id = str(dataset_id)
    images, labels, skipped = 0, 0, 0
    for name in zf.namelist():
        stripped = name.strip("/")
        if name.endswith("/") or stripped.startswith("__MACOSX"):
            continue
        if stripped.startswith("images/") and stripped.lower().endswith(_IMAGE_EXTS):
            lakefs_client.upload(ds_id, stripped, zf.read(name))
            images += 1
        elif stripped.startswith("labels/") and stripped.endswith(".txt"):
            lakefs_client.upload(ds_id, stripped, zf.read(name))
            labels += 1
        elif stripped in ("classes.txt", "obj.names"):
            lakefs_client.upload(ds_id, "classes.txt", zf.read(name))
        else:
            skipped += 1

    if images == 0:
        raise HTTPException(status_code=400, detail="zip has no images/<file> entries")
    return {"images": images, "labels": labels, "skipped": skipped}


@router.post("/{dataset_id}/upload-coco")
async def upload_coco(
    project_id: uuid.UUID, dataset_id: uuid.UUID, file: UploadFile, tenant_id: str = Depends(get_tenant_id)
):
    """FR2: a .zip with COCO `annotations.json` (images[], annotations[]
    with `bbox: [x, y, w, h]` in pixels, `category_id`) plus the image
    files themselves. Converted into the same YOLO-style
    `images/<file>` + `labels/<file>.txt` representation upload-yolo
    produces (normalized cx/cy/w/h), so the rest of the platform — preview,
    training — only ever deals with one on-disk box format."""
    async with TenantConn(tenant_id) as conn:
        await _get_dataset(conn, dataset_id, project_id)
        await _check_task_type_gate(conn, project_id, "upload-coco")

    data = await file.read()
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="not a valid zip file")

    ann_name = next((n for n in zf.namelist() if n.strip("/").lower().endswith("annotations.json")), None)
    if ann_name is None:
        raise HTTPException(status_code=400, detail="zip has no annotations.json")
    try:
        coco = json.loads(zf.read(ann_name).decode())
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="annotations.json is not valid JSON")

    images_by_id = {img["id"]: img for img in coco.get("images", [])}
    categories = sorted({c["id"] for c in coco.get("categories", [])})
    cat_index = {cat_id: i for i, cat_id in enumerate(categories)}
    boxes_by_image: dict[int, list[str]] = {}
    for ann in coco.get("annotations", []):
        img = images_by_id.get(ann["image_id"])
        if img is None:
            continue
        x, y, w, h = ann["bbox"]
        cx = (x + w / 2) / img["width"]
        cy = (y + h / 2) / img["height"]
        nw, nh = w / img["width"], h / img["height"]
        cls = cat_index[ann["category_id"]]
        boxes_by_image.setdefault(ann["image_id"], []).append(f"{cls} {cx:.6f} {cy:.6f} {nw:.6f} {nh:.6f}")

    ds_id = str(dataset_id)
    images, labels = 0, 0
    for name in zf.namelist():
        stripped = name.strip("/")
        if name.endswith("/") or not stripped.lower().endswith(_IMAGE_EXTS):
            continue
        filename = stripped.rsplit("/", 1)[-1]
        img_id = next((i for i, img in images_by_id.items() if img["file_name"].endswith(filename)), None)
        if img_id is None:
            continue
        lakefs_client.upload(ds_id, f"images/{filename}", zf.read(name))
        images += 1
        stem = filename.rsplit(".", 1)[0]
        lakefs_client.upload(ds_id, f"labels/{stem}.txt", "\n".join(boxes_by_image.get(img_id, [])).encode())
        labels += 1

    if images == 0:
        raise HTTPException(status_code=400, detail="no images in the zip matched annotations.json")
    return {"images": images, "labels": labels, "categories": len(categories)}


@router.post("/{dataset_id}/tabular")
async def upload_tabular(
    project_id: uuid.UUID, dataset_id: uuid.UUID, request: Request, tenant_id: str = Depends(get_tenant_id)
):
    """Raw feature/label CSV for the Ludwig backend's default (non-HF)
    path (see app/ray_client.py) — arbitrary columns, not the fixed
    image_id/image_path/class_no shape `manifest` uses. If a column looks
    like an image path, those files still need to actually exist in this
    dataset — upload them via the normal upload-url/upload-zip flow using
    the same paths this CSV references."""
    async with TenantConn(tenant_id) as conn:
        await _get_dataset(conn, dataset_id, project_id)
        await _check_task_type_gate(conn, project_id, "tabular")

    raw = await request.body()
    reader = csv.DictReader(io.StringIO(raw.decode()))
    columns = reader.fieldnames or []
    if not columns:
        raise HTTPException(status_code=400, detail="CSV has no header row")
    rows = list(reader)
    if not rows:
        raise HTTPException(status_code=400, detail="CSV has no data rows")

    lakefs_client.upload(str(dataset_id), "tabular.csv", raw)
    return {"rows": len(rows), "columns": columns}


@router.get("/{dataset_id}/tabular")
async def get_tabular(
    project_id: uuid.UUID, dataset_id: uuid.UUID, tenant_id: str = Depends(get_tenant_id), ref: str = ""
):
    async with TenantConn(tenant_id) as conn:
        await _get_dataset(conn, dataset_id, project_id)
    try:
        raw = lakefs_client.download(str(dataset_id), "tabular.csv", ref=ref)
    except Exception:  # noqa: BLE001 - no tabular data uploaded yet
        raise HTTPException(status_code=404, detail="no tabular data uploaded for this dataset")
    rows = list(csv.DictReader(io.StringIO(raw.decode())))
    return {"columns": list(rows[0].keys()) if rows else [], "rows": rows[:50], "total_rows": len(rows)}


@router.post("/{dataset_id}/import-hf", status_code=202)
async def import_hf_dataset(
    project_id: uuid.UUID,
    dataset_id: uuid.UUID,
    tenant_id: str = Depends(get_tenant_id),
    hf_dataset: str = Body(embed=True),
    kind: str = Body(embed=True),
    image_column: str = Body(default="image", embed=True),
    label_column: str = Body(default="label", embed=True),
    text_column: str = Body(default="text", embed=True),
    max_rows: int | None = Body(default=2000, embed=True),
):
    """Materialize a real Hugging Face Hub dataset into this dataset's own
    (versioned) storage — kind="images" writes real files into the
    standard <label>/<file> layout (immediately trainable with the
    Ultralytics backend), kind="tabular" writes tabular.csv (immediately
    trainable with the Ludwig default backend). Runs as a background Ray
    job (see app/ray_client.py's submit_hf_import) since it needs network
    access and the `datasets` library core-api itself doesn't carry."""
    if kind not in ("images", "tabular"):
        raise HTTPException(status_code=400, detail="kind must be 'images' or 'tabular'")
    async with TenantConn(tenant_id) as conn:
        await _get_dataset(conn, dataset_id, project_id)
        await _check_task_type_gate(conn, project_id, f"import-hf-{kind}")

        ray_job_id = await ray_client.submit_hf_import(
            hf_dataset=hf_dataset,
            kind=kind,
            s3_prefix=str(dataset_id),
            image_column=image_column,
            label_column=label_column,
            text_column=text_column,
            max_rows=max_rows,
            tenant_id=tenant_id,
            project_id=str(project_id),
            use_lakefs=True,
        )
        row = await conn.fetchrow(
            "INSERT INTO dataset_imports (tenant_id, project_id, dataset_id, ray_job_id, hf_dataset, kind) "
            "VALUES ($1, $2, $3, $4, $5, $6) RETURNING id, status, ray_job_id, hf_dataset, kind, created_at",
            tenant_id,
            project_id,
            dataset_id,
            ray_job_id,
            hf_dataset,
            kind,
        )
    return dict(row)


@router.get("/{dataset_id}/imports")
async def list_dataset_imports(
    project_id: uuid.UUID, dataset_id: uuid.UUID, tenant_id: str = Depends(get_tenant_id)
):
    async with TenantConn(tenant_id) as conn:
        rows = await conn.fetch(
            "SELECT id, status, hf_dataset, kind, error, created_at, completed_at FROM dataset_imports "
            "WHERE dataset_id = $1 AND project_id = $2 ORDER BY created_at DESC",
            dataset_id,
            project_id,
        )
    return [dict(r) for r in rows]


async def reconcile_inflight_imports() -> None:
    """Same pattern as app/training.py's reconcile_inflight_runs — nothing
    else polls Ray for an import's status, so without this a running
    import would sit at 'submitted'/'running' forever in the DB even
    after Ray finished it."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        tenant_ids = [r["inflight_import_tenant_ids"] for r in await conn.fetch("SELECT inflight_import_tenant_ids()")]

    ray_to_status = {
        "PENDING": "submitted",
        "RUNNING": "running",
        "SUCCEEDED": "succeeded",
        "FAILED": "failed",
        "STOPPED": "failed",
    }
    for tenant_id in tenant_ids:
        async with TenantConn(str(tenant_id)) as conn:
            rows = await conn.fetch(
                "SELECT * FROM dataset_imports WHERE tenant_id = $1 AND status IN ('submitted', 'running')",
                tenant_id,
            )
            for row in rows:
                try:
                    ray_status = await ray_client.get_status(row["ray_job_id"])
                except Exception:  # noqa: BLE001 - one bad job shouldn't block the sweep
                    logger.exception("failed to poll ray status for import %s", row["id"])
                    continue
                new_status = ray_to_status.get(ray_status, row["status"])
                if new_status == row["status"]:
                    continue
                error = None
                if new_status == "failed":
                    logs = await ray_client.get_logs(row["ray_job_id"])
                    error = logs[-2000:]
                await conn.execute(
                    "UPDATE dataset_imports SET status = $2, error = $3, "
                    "completed_at = CASE WHEN $2 IN ('succeeded', 'failed') THEN now() ELSE completed_at END "
                    "WHERE id = $1",
                    row["id"],
                    new_status,
                    error,
                )


@router.get("/{dataset_id}/preview")
async def preview_dataset(
    project_id: uuid.UUID,
    dataset_id: uuid.UUID,
    tenant_id: str = Depends(get_tenant_id),
    limit: int = Query(default=60, ge=1, le=200),
    ref: str = "",
):
    """Thumbnail gallery for the UI — same class-label folder convention
    the Ultralytics backend trains on (key = "<label>/<file>"), so this
    doubles as a sanity check that what you uploaded is what the job
    will actually see. `ref` optionally pins this to a specific
    committed version instead of the current working state."""
    async with TenantConn(tenant_id) as conn:
        await _get_dataset(conn, dataset_id, project_id)

    ds_id = str(dataset_id)
    keys = lakefs_client.list_files(ds_id, ref=ref)
    images = [k for k in keys if k.lower().endswith(_IMAGE_EXTS) and not k.startswith("_unlabeled/")]
    by_label: dict[str, int] = {}
    items = []
    for key in images:
        label = key.split("/", 1)[0]
        by_label[label] = by_label.get(label, 0) + 1
        if len(items) < limit:
            items.append({"key": key, "label": label, "view_url": lakefs_client.presigned_get_url(ds_id, key, ref=ref)})
    return {"total_files": len(keys), "total_images": len(images), "labels": by_label, "items": items}


@router.get("/{dataset_id}/files")
async def list_dataset_files(
    project_id: uuid.UUID, dataset_id: uuid.UUID, tenant_id: str = Depends(get_tenant_id), ref: str = ""
):
    async with TenantConn(tenant_id) as conn:
        await _get_dataset(conn, dataset_id, project_id)
    return {"files": lakefs_client.list_files(str(dataset_id), ref=ref)}


@router.post("/{dataset_id}/versions", status_code=201)
async def commit_dataset_version(
    project_id: uuid.UUID, dataset_id: uuid.UUID, tenant_id: str = Depends(get_tenant_id), message: str = Body(embed=True)
):
    """Real data versioning (req from this session): snapshots the
    dataset's current file state as a LakeFS commit. Training runs can
    then pin to a specific version instead of always training on
    whatever's currently uploaded — see app/training.py's dataset_version
    param."""
    async with TenantConn(tenant_id) as conn:
        await _get_dataset(conn, dataset_id, project_id)
    if not lakefs_client.has_uncommitted_changes(str(dataset_id)):
        raise HTTPException(status_code=409, detail="no changes since the last version — nothing to commit")
    return lakefs_client.commit(str(dataset_id), message)


@router.post("/{dataset_id}/publish")
async def publish_dataset(
    project_id: uuid.UUID,
    dataset_id: uuid.UUID,
    published: bool = Body(embed=True),
    tenant_id: str = Depends(get_tenant_id),
):
    """FR6 (08-collaboration-visibility.md): publishes the *current* LakeFS
    commit, not a live pointer — a later upload doesn't become public until
    re-published here."""
    async with TenantConn(tenant_id) as conn:
        await _get_dataset(conn, dataset_id, project_id)
        published_ref = None
        if published:
            versions = lakefs_client.list_versions(str(dataset_id))
            if not versions:
                raise HTTPException(status_code=409, detail="dataset has no committed version to publish")
            published_ref = versions[0]["id"]
        row = await conn.fetchrow(
            "UPDATE datasets SET published = $2, published_ref = $3 WHERE id = $1 "
            "RETURNING id, published, published_ref",
            dataset_id,
            published,
            published_ref,
        )
    return dict(row)


@router.get("/{dataset_id}/versions/{commit_id}/training-runs")
async def get_versions_training_runs(
    project_id: uuid.UUID, dataset_id: uuid.UUID, commit_id: str, tenant_id: str = Depends(get_tenant_id)
):
    """FR6 (05-reproducibility-lineage.md): every run ever pinned to this
    exact commit — either as the training set or the test set — so an
    owner sees what re-committing this dataset would affect before
    finding out the hard way."""
    async with TenantConn(tenant_id) as conn:
        await _get_dataset(conn, dataset_id, project_id)
        rows = await conn.fetch(
            "SELECT id, status, created_at FROM training_runs WHERE project_id = $1 AND "
            "((dataset_id = $2 AND dataset_version = $3) OR (test_dataset_id = $2 AND test_dataset_version = $3)) "
            "ORDER BY created_at DESC",
            project_id,
            dataset_id,
            commit_id,
        )
    return [dict(r) for r in rows]


@router.get("/{dataset_id}/versions")
async def list_dataset_versions(project_id: uuid.UUID, dataset_id: uuid.UUID, tenant_id: str = Depends(get_tenant_id)):
    async with TenantConn(tenant_id) as conn:
        await _get_dataset(conn, dataset_id, project_id)
    versions = lakefs_client.list_versions(str(dataset_id))
    has_uncommitted = lakefs_client.has_uncommitted_changes(str(dataset_id))
    return {"versions": versions, "has_uncommitted_changes": has_uncommitted}
