# Labeling and preprocessing pipelines

## Labeling: use Label Studio, don't build one
Label Studio has been running in `docker-compose.yml` since Phase 1
(`labelstudio` service, admin credentials already provisioned) and was
never wired to the dataset flow — Phase 1 instead built a *minimal*
in-app labeling loop (`app/datasets.py`'s `label_file` endpoint: move an
`_unlabeled/` upload into a class folder) as a stopgap for whole-image
classification only. Phase 2 promotes real labeling to Label Studio and
keeps the in-app loop only as the "I have three images, don't make me
leave the page" fast path.

FR1: When a dataset has files under `_unlabeled/` (already the existing
convention) and the project's `task_type` needs structured annotation
(`object_detection` bounding boxes, or classification at real volume),
"Send to Label Studio" creates a Label Studio project via its REST API,
pointed at the dataset's presigned-URL-readable LakeFS objects as the
task source.
FR2: Label Studio's export (JSON) is pulled back via a `POST
.../datasets/{id}/import-label-studio-export` endpoint — same
parse-and-write-to-LakeFS shape as the existing YOLO/COCO importers (03,
FR2) — completed labels become a real LakeFS commit, not a live sync.
FR3: This is genuinely additive: `app/labeling.py` already exists as a
stub per the codebase's own module layout; Phase 2 fills it in rather
than adding a new module.

## Preprocessing: Ray Data + Ludwig-shaped config, not a new DSL
The ask is "let users modify/preprocess their dataset with some automated
process — suggest a low-code framework." The framework already in this
stack that *is* a low-code preprocessing spec is Ludwig's own
`preprocessing` config block (numeric normalization, missing-value fill,
tokenization, image resize/augment — all declarative YAML keys Ludwig
already implements and documents). Reusing it means:

FR4: A "Preprocess dataset" UI step generates a small JSON object shaped
like a subset of Ludwig's `preprocessing` schema (resize, normalize,
dedupe-near-identical, class-balance via oversample/undersample, missing
value strategy) — the same shape already partially used by
`_LUDWIG_TEMPLATE`'s `preprocessing.split` block (`app/ray_client.py`).
FR5: That JSON is submitted as a Ray job (same `submit_raw_job` path
every other backend job uses) whose entrypoint is a small, hand-written
executor — not Ludwig itself, since preprocessing here produces a new
*dataset* (new LakeFS commit) rather than feeding straight into training.
The executor is Ray Data (`ray.data.read_images` /
`.map_batches(...)` / write back to the LakeFS S3 gateway) — chosen
because it's already installed (Ray's own library, zero new dependency)
and expresses "read → transform → write" without a DAG engine.
FR6: Preprocessing output is a **new dataset version** (a LakeFS commit
on either the same dataset's branch, tagged distinctly, or a new
dataset — TBD in implementation, leaning "same branch, new commit" since
05's lineage model already handles "which commit did this training run
use" and a same-branch commit keeps the dataset identity stable).

## Why not a general low-code framework (e.g., a full node-graph builder)
A drag-drop node graph (à la n8n/ComfyUI-style pipelines) is real
engineering surface for a v1 feature set that's currently "resize +
normalize + balance + dedupe" — four fixed, well-understood operations.
Ship those four as form fields first; revisit a graph UI only once
customer demand names a fifth operation that doesn't fit a flat list.
