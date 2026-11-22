# Trainer plan: image classification and object detection

Status: built (the first slice shipped; this page keeps the original design and decisions). Where the code differs: training runs as a Ray Train `TorchTrainer` (`ray_core/entrypoints/train.py`, `ray_core/worker.py`) rather than a bare Ray task, the package is `ray-training/ray_core` (staged under the name `ray_training`), and each task's trainer is a bundle in `task-bundles/tasks/<slug>/`. See `docs/ray-parallelism.md`, `docs/job-spec.md` and `../planning/25-task-bundles.md`. Scope at the time, from the owner: both tasks in the first trainer slice, one GPU plus a CPU pool, `report.json` instead of the events API for now. Builds on planning 06, 07, 16, 17 and `docs/datasets-plan.md`; where this page differs from them it says so.

## Decisions

| # | Decision | Why |
|---|---|---|
| T1 | Trainer reads `index.parquet` (columns `path, size, sha256, label, boxes, split`) through presigned GETs. No WebDataset, no node cache | Ingest already writes it; shards are deferred in the datasets plan. Revisit when a dataset no longer fits one download |
| T2 | Results come back as `report.json` in a staging prefix, written by one shared `run()` like `ray_jobs/runner.py`. No events API, no log parsing | Reuses the ingest pattern. Progress is coarse (queued, running, done) until the events API exists |
| T3 | Trainer code runs as a **Ray task** on the GPU worker, started by a CPU-side driver that calls `ray.init(address="auto")` | See "Ray execution" below |
| T4 | Classification: HF `transformers` (ConvNeXt, ViT) or `timm`. Detection: `RTDetrV2ForObjectDetection` (ADR-016, no AGPL). Both export ONNX | Doc 06 |
| T5 | Presets are rows of `task_presets` (base model pinned to a commit, learning rate, preprocessing, cpu/memory/gpu/max runtime, hyperparameter defaults and bounds), changed through `/v1/admin/training-presets` without a deploy. Names are free per task. A run copies the values into its job spec, so an edit never changes a past run. The trainer keeps no table | Presets change more often than code (a revision bump, a new model); the spec already carried the values |
| T6 | One hardcoded base model per preset, pinned to a commit, in `task_catalog` and sent in the job spec. Catalog admission (doc 17) is deferred | The catalog is a separate slice |
| T7 | The trainer holds no MLflow or lakeFS credential. core-api reads `report.json`, logs metrics to MLflow, registers the model version | Doc 06, 16 |

## Ray execution (the `ray.init` question)

The cluster (k8s `apps/ray/app/raycluster.yaml`): head with `num-cpus: 0`, one `gpu` worker group (1 replica, RTX 5090, `cu128` image), one `cpu` worker group advertising `cpu_pool`. Ingest jobs are plain Python run as the job *entrypoint*; they never call `ray.init`.

Training must, because the GPU belongs to the worker, not the driver:

```python
# ray_core/entrypoints/train.py  (driver, runs on the CPU pool: entrypointResources={"cpu_pool": 1}, entrypointNumGpus=0)
ray.init(address="auto")                      # join the cluster the job was submitted to
ref = train.options(num_gpus=1, num_cpus=8, memory=...).remote(spec)   # runs on the GPU worker
report = ray.get(ref)                          # blocks; a cancelled job raises here
```

- `entrypoint_num_gpus` stays 0. If the driver reserved the only GPU, the remote task could never schedule.
- `num_gpus=1` is a whole card: a second run waits in Ray's queue (PENDING), which is the first-slice concurrency model. Serving on the same card needs a fraction; that is doc 07's "GPU memory and sharing", not this slice.
- The driver passes the job's runtime env to the task, so the zipped job code (job_package.py) reaches the GPU worker as it reaches the CPU worker. The GPU worker image needs the trainer dependencies baked in (torch, transformers, timm, onnx, onnxruntime, pyarrow, boto3): a `trainer-vision` image on the `cu128` base, and a `k8s` image bump. **Cross-repo change.**
- Failure: the task catches everything and still returns a failed report; the driver writes `report.json` in a `finally`, so OOM and bad data end in a terminal state. A killed worker surfaces as `ray.exceptions.RayTaskError` / `WorkerCrashedError` in the driver, which writes `failed` with code `internal`.
- Timeout: the same SIGALRM pattern as ingest, in the driver; the task is cancelled with `ray.cancel(ref, force=True)`.

## Job spec v1: `kind: train`

Envelope unchanged (`spec_version, job_id, tenant_id, resources`); `resources.gpu = 1`. Payload:

```json
{
  "bucket": "<staging bucket>", "prefix": "trainings/<tenant>/<project>/<run>/",
  "task": "image-classification | object-detection",
  "preset": "small | medium | large",
  "base_model": {"repo_id": "...", "revision": "<sha>"},
  "data": {"index_url": "<presigned GET>", "objects_base_url": "<presigned GET prefix or per-object map>", "version_id": "...", "lakefs_commit": "..."},
  "hyperparameters": {"epochs": 30, "batch_size": 16, "image_size": 224},
  "seed": 42
}
```

New in core-api: a `TrainSpec` beside `IngestSpec` (discriminated on `kind`), a regenerated `contracts/job-spec.v1.json`, and a trainer-side mirror kept equal by a contract test, as for ingest.

Open: presigned GETs per image do not scale to thousands of files in one spec. Options: the trainer gets a read-only credential scoped to the dataset's lakeFS-backed prefix (needs E6's STS work), or core-api puts a manifest of presigned URLs in staging. Pick the manifest for the first slice.

## Outputs (`report.json` plus artifacts)

Under the run prefix: `model.onnx`, `model.json`, `report.json`.

- `model.json`: format, input/output signature, class labels, preprocessing, trainer version, dataset `lakefs_commit`, metrics summary.
- `report.json`: status, failure code (`oom`, `bad_data`, `unsupported`, `timeout`, `internal`), message, scalar metrics, per-epoch history, confusion matrix (classification), artifact keys.
- Fixed metrics: classification top-1, top-5, confusion matrix; detection mAP50, mAP50-95. Splits: the dataset's `split` column where set, else a deterministic hash of `sha256` (doc 06 quality gates).

## core-api changes

1. `POST /v1/projects/{id}/training-runs` takes `dataset_version_id` + `preset` (+ optional hyperparameters), checks the version's task matches the project, builds `TrainSpec`, submits through `compute.submit` with `num_gpus=0` on the driver (see above), stores the spec.
2. `training_runs` migration: add `dataset_version_id`, `preset`, `spec jsonb`, `report jsonb`; drop what log parsing needed. Enum `CHECK`s mirrored (status, preset, task).
3. `service.transition`: replace `log_parsing` with reading `report.json` via the data-plane gateway; register the model version on success (existing `model_registry`, MLflow through the gateway).
4. Delete `log_parsing.py`, `/logs` metrics parsing (keep a raw log tail for debugging; it needs the `pods/log` RBAC, gap E7).
5. Usage: GPU-seconds already metered on success; keep, key on the job id.

## Build order (each step ends with the suite green)

| # | Step | Done when |
|---|---|---|
| 1 | `TrainSpec` + schema + contract test | Schema test equals the committed JSON; a bad preset is rejected |
| 2 | `ray-training/` package: runner (`report.json` always), classification trainer with a tiny CPU fixture, ONNX export | Unit test: train 1 epoch on 8 images on CPU, ONNX loads in onnxruntime, report has metrics |
| 3 | Driver with `ray.init(address="auto")` and the GPU task; run it against a local Ray (`ray start --head`) with `num_gpus=0` for CI | A Ray test submits the entrypoint and gets `report.json` |
| 4 | core-api: migration, create route, reconcile from `report.json`, registry write | Route x role matrix; foreign project 404; double settle is idempotent |
| 5 | Detection (RT-DETRv2 from `boxes`), mAP metrics, ONNX with DETR-style output | Same fixture-level test with a 4-image COCO set |
| 6 | `trainer-vision` image + `k8s` bump (ask before touching `k8s`) and one real run on the 5090 | Walking skeleton: upload, train, see model version |

## Risks and unknowns

- RT-DETRv2 ONNX export is documented but untested here; if it fails, detection slips (doc 20 slip rule: it is the first to go).
- `cu128` Ray image plus torch wheels: check the 5090 (Blackwell, sm_120) is supported by the torch build before baking the image.
- One GPU means one run at a time and no serving on it; say so in the UI.
- Presigned manifest size and expiry for long runs: TTL must exceed `max_runtime_s`.
