# ML training — MLflow, Ludwig, Ultralytics

## Backend split (decided in prior discussion)
| Task | Backend | Reason |
|---|---|---|
| Classification, tabular, time series (later) | Ludwig | Declarative YAML config maps cleanly to drag-drop blocks; fixed-shape outputs fit its encoder-combiner-decoder schema |
| Detection, segmentation | Ultralytics (YOLO family) | Variable-length / dense spatial outputs Ludwig's schema can't express |
| Custom/imported backbones | HuggingFace Hub + `transformers`/`timm` (classification), torchvision pretrained encoders (via Ludwig) | Wrapped behind one internal `ModelAdapter` interface (load/fit/predict/export) so the UI doesn't care which library backs a given choice |

## Dataset ingestion into training
- CSV/manifest with `image_id, image_path, class_no` (or bbox/mask equivalents for detect/segment) — image paths point at platform-owned object storage, not live external URLs (see prior discussion — reproducibility + no third-party dependency at train time).
- Split computed once at dataset-version creation, stored as a manifest column, referenced by every training run against that dataset version — never regenerated randomly per run.

## MLflow requirements
- FR1: Every training run (Ludwig or Ultralytics) logs params, metrics, and the resulting model artifact to MLflow tracking, tagged with `tenant_id`, `project_id`, `dataset_commit`.
- FR2: Model Registry stages map to customer-facing states: `None` (internal only) → `Staging` (shown as "candidate" in UI) → `Production` (shown as "live/deployed").
- FR3: Registry entry must store a pointer back to the dataset version (LakeFS commit, once introduced — see phasing) it was trained on, for lineage/audit.
- NFR1: MLflow tracking server backed by Postgres (existing data layer) + S3/MinIO artifact store — no separate DB technology.

## Ludwig requirements
- FR1: Platform-generated Ludwig YAML from UI block state must be stored alongside the model version (for reproducibility and "export config" feature).
- FR2: Default to pretrained encoder + feature-extraction (`trainable: false`) for small customer datasets; expose full finetune (`trainable: true`) only past a dataset-size threshold (TBD, likely >500 images) or in advanced mode.
- FR3: Support Ludwig's Ray backend (`backend: ray` in config) so Ludwig training jobs distribute across the same Ray cluster used for Ultralytics/Ray Tune — one compute layer, not two.

## Ultralytics requirements
- FR1: Dataset export from platform manifest to YOLO-format directory structure (images/ + labels/ in YOLO txt format) as a pipeline step before training.
- FR2: Support detect/segment/classify task selection from one unified training entrypoint.
- FR3: Export trained weights to ONNX at minimum, for BentoML/Ray Serve packaging.

## Open questions
- Threshold for "too little data to train a useful model" — platform should refuse or warn rather than silently produce a bad model for a non-technical user who can't judge quality themselves. Needs a concrete minimum-images-per-class rule, TBD.
