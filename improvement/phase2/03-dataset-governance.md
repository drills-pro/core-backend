# Dataset governance — task-typed, minimum-sample, train/test pairing

## Task-type-matched ingestion
FR1: Every dataset attach/import endpoint (`upload-url`, `upload-zip`,
`tabular`, `import-hf`) validates its payload shape against the owning
project's `task_type` (02) before accepting:
| task_type | accepted shapes |
|---|---|
| `image_classification` | label-folder images (existing upload-zip/upload-url convention), HF `kind="images"` import, Ludwig image-column tabular |
| `object_detection` | **new**: YOLO format (`images/`, `labels/*.txt`) or COCO JSON — see FR2 | 
| `tabular_classification` | tabular CSV upload, HF `kind="tabular"` import |
| `text_classification` | HF text-column import (existing Ludwig HF path) |

FR2 (new capability, not present in Phase 1): a YOLO/COCO import endpoint,
`POST .../datasets/{id}/upload-yolo` and `.../upload-coco` — same
server-side-unzip-and-relay-to-LakeFS shape as the existing
`upload-zip` (`app/datasets.py`), just parsing `labels/*.txt` or a COCO
`annotations.json` into the platform's own label representation
(`labels/<image>.txt` stored alongside, LakeFS-versioned like everything
else) instead of re-deriving folder-name labels. This is what makes
`object_detection` projects accept real bounding-box ground truth —
closing the gap Phase 1's detect backend explicitly documented (synthetic
boxes only, no annotation import path).

## Minimum sample gate
FR3: A dataset (or a dataset+its paired test set, combined) must have
≥10 labeled samples before a training run can target it. Enforced in
`create_training_run` (`app/training.py`), returning 422 with the actual
count, not a generic error — the count is cheap (`lakefs_client.list_files`
already returns the full key list Phase 1 uses for `real_files_used`).
FR4: The Datasets tab surfaces this as a live counter ("7 / 10 minimum")
rather than letting the user discover it at submit time.

## Train/test pairing
FR5: This is already built (Phase 1 shipped `test_dataset_id` /
`test_dataset_version` on `training_runs`, wired through Ultralytics
classify and Ludwig tabular). Phase 2's job here is UI, not backend:
promote the test-dataset picker from "optional field in a training form"
to a first-class per-project setting — a project can have a *designated*
default test dataset (stored on `projects.default_test_dataset_id`) that
pre-fills every training run's test-set picker, so "did you use a
consistent test set across your last 5 runs" stops being something the
user has to remember.
FR6: When no test dataset is designated (default or per-run), the
existing internal `test_split` random-split behavior is the fallback —
unchanged from Phase 1.

## Reproduction guarantee
FR7: This is also already built via LakeFS commits (`app/lakefs_client.py`)
— restated here because it's the property dataset governance exists to
protect: any `training_runs.dataset_version` + `test_dataset_version` pair
is a fully reproducible input, forever, as long as the LakeFS repo isn't
garbage-collected. Phase 2 adds no new mechanism, just a UI affordance
(06) to re-submit a run against the exact version pair a prior run used.
