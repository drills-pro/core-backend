import uuid

from fastapi import APIRouter, Body, Depends

from app.core.authz import Action, AuthzContext, require
from app.core.enums import TaskType
from app.core.problem import not_found
from app.core.tenant_conn import TenantConn
from app.integrations import lakefs_client, labelstudio_client

router = APIRouter(prefix="/v1/projects/{project_id}/datasets/{dataset_id}/labeling", tags=["labeling"])

# FR1 (04-labeling-preprocessing.md): task-type-aware config — bounding
# boxes for object_detection, otherwise the classification Choices config
# Phase 1 shipped, generalized to whatever labels are already present as
# class folders (falls back to two placeholder classes for a brand new
# dataset with nothing labeled yet).
_CLASSIFY_CONFIG = """
<View>
  <Image name="image" value="$image"/>
  <Choices name="label" toName="image">
{choices}
  </Choices>
</View>
"""
_BBOX_CONFIG = """
<View>
  <Image name="image" value="$image"/>
  <RectangleLabels name="label" toName="image">
{choices}
  </RectangleLabels>
</View>
"""


async def _existing_labels(dataset_id: uuid.UUID) -> list[str]:
    keys = lakefs_client.list_files(str(dataset_id))
    labels = sorted({k.split("/", 1)[0] for k in keys if "/" in k and not k.startswith("_unlabeled/")})
    return labels or ["classA", "classB"]


@router.post("", status_code=201)
async def create_labeling_project(
    project_id: uuid.UUID, dataset_id: uuid.UUID, ctx: AuthzContext = Depends(require(Action.DATASET_WRITE))
):
    async with TenantConn(ctx.tenant_id) as conn:
        dataset = await conn.fetchrow(
            "SELECT name FROM datasets WHERE id = $1 AND project_id = $2", dataset_id, project_id
        )
        if dataset is None:
            raise not_found()
        task_type = await conn.fetchval("SELECT task_type FROM projects WHERE id = $1", project_id)

        labels = await _existing_labels(dataset_id)
        template = _BBOX_CONFIG if task_type == TaskType.OBJECT_DETECTION else _CLASSIFY_CONFIG
        tag = "Label" if task_type == TaskType.OBJECT_DETECTION else "Choice"
        choices = "\n".join(f'    <{tag} value="{label}"/>' for label in labels)
        label_config = template.format(choices=choices)

        # Label Studio caps project titles at 50 chars.
        title = f"{dataset['name']}-{str(dataset_id)[:8]}"[:50]
        ls_project_id = await labelstudio_client.create_project(title=title, label_config=label_config)

        # FR1: point Label Studio at the dataset's unlabeled uploads via
        # presigned-URL-readable LakeFS objects — the existing "raw upload"
        # convention (app/modules/datasets/routes.py's unlabeled-upload-url).
        unlabeled_keys = lakefs_client.list_files(str(dataset_id), prefix="_unlabeled/")
        tasks = [
            {"image": lakefs_client.presigned_get_url(str(dataset_id), k), "key": k} for k in unlabeled_keys
        ]
        if tasks:
            await labelstudio_client.import_tasks(ls_project_id, tasks)

        row = await conn.fetchrow(
            "INSERT INTO labeling_projects (tenant_id, project_id, dataset_id, label_studio_id) "
            "VALUES ($1, $2, $3, $4) RETURNING id, label_studio_id, created_at",
            ctx.tenant_id,
            project_id,
            dataset_id,
            ls_project_id,
        )
    return {
        **dict(row),
        "tasks_imported": len(tasks),
        "project_url": f"{labelstudio_client.PUBLIC_ENDPOINT}/projects/{ls_project_id}/data",
    }


@router.get("")
async def get_labeling_status(
    project_id: uuid.UUID, dataset_id: uuid.UUID, ctx: AuthzContext = Depends(require(Action.DATASET_READ))
):
    async with TenantConn(ctx.tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT label_studio_id FROM labeling_projects WHERE project_id = $1 AND dataset_id = $2 "
            "ORDER BY created_at DESC LIMIT 1",
            project_id,
            dataset_id,
        )
    if row is None:
        raise not_found()
    stats = await labelstudio_client.get_project_stats(row["label_studio_id"])
    return {
        "label_studio_id": row["label_studio_id"],
        "project_url": f"{labelstudio_client.PUBLIC_ENDPOINT}/projects/{row['label_studio_id']}/data",
        **stats,
    }


@router.get("/export")
async def export_labels(project_id: uuid.UUID, dataset_id: uuid.UUID, ctx: AuthzContext = Depends(require(Action.DATASET_READ))):
    """FR: "pull labeled export"."""
    async with TenantConn(ctx.tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT label_studio_id FROM labeling_projects WHERE project_id = $1 AND dataset_id = $2 "
            "ORDER BY created_at DESC LIMIT 1",
            project_id,
            dataset_id,
        )
    if row is None:
        raise not_found()
    return await labelstudio_client.export_annotations(row["label_studio_id"])


@router.post("/import-label-studio-export")
async def import_label_studio_export(
    project_id: uuid.UUID, dataset_id: uuid.UUID, ctx: AuthzContext = Depends(require(Action.DATASET_WRITE))
):
    """FR2: pull the current Label Studio export and write completed
    labels back to LakeFS as a real commit — same parse-and-write shape as
    03's YOLO/COCO importers, not a live sync. Classification annotations
    move the file from _unlabeled/ into its class folder (Phase 1's
    existing convention); object_detection annotations write a YOLO-style
    labels/<file>.txt."""
    async with TenantConn(ctx.tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT label_studio_id FROM labeling_projects WHERE project_id = $1 AND dataset_id = $2 "
            "ORDER BY created_at DESC LIMIT 1",
            project_id,
            dataset_id,
        )
        if row is None:
            raise not_found()
        task_type = await conn.fetchval("SELECT task_type FROM projects WHERE id = $1", project_id)

    export = await labelstudio_client.export_annotations(row["label_studio_id"])
    ds_id = str(dataset_id)
    labeled = 0
    for task in export:
        key = task.get("data", {}).get("key")
        annotations = task.get("annotations") or []
        if not key or not annotations or not key.startswith("_unlabeled/"):
            continue
        results = annotations[0].get("result") or []
        if not results:
            continue
        filename = key.rsplit("/", 1)[-1]

        if task_type == TaskType.OBJECT_DETECTION:
            # ponytail: class_id hardcoded to 0 — no per-project class-name
            # registry exists yet to map rectanglelabels[0] to an index;
            # add one (and a real classes.txt) once multi-class detection
            # projects need it.
            lines = []
            for r in results:
                v = r.get("value", {})
                labels = v.get("rectanglelabels") or []
                if not labels or "width" not in v:
                    continue
                cx = (v["x"] + v["width"] / 2) / 100
                cy = (v["y"] + v["height"] / 2) / 100
                lines.append(f"0 {cx:.6f} {cy:.6f} {v['width']/100:.6f} {v['height']/100:.6f}")
            if not lines:
                continue
            stem = filename.rsplit(".", 1)[0]
            lakefs_client.upload(ds_id, f"labels/{stem}.txt", "\n".join(lines).encode())
            lakefs_client.copy(ds_id, key, f"images/{filename}")
            lakefs_client.delete(ds_id, key)
        else:
            choices = results[0].get("value", {}).get("choices") or []
            if not choices:
                continue
            lakefs_client.copy(ds_id, key, f"{choices[0]}/{filename}")
            lakefs_client.delete(ds_id, key)
        labeled += 1

    if labeled and lakefs_client.has_uncommitted_changes(ds_id):
        lakefs_client.commit(ds_id, f"import {labeled} labels from Label Studio export")
    return {"labeled": labeled}
