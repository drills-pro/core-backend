# Where Ray parallelism is used, and what is next

Status 2026-11-10. "Built" rows were run on the local stack (`scripts/e2e-journey.sh`) unless noted. "Designed" rows are
not built; they need the owner's go-ahead because each adds a job kind or a product surface.

## Built

| Area | Ray feature | What it buys | Where |
|---|---|---|---|
| Training | Ray Train `TorchTrainer`, data parallel, resumable | one run spreads across the workers a preset asks for; a killed worker resumes from a checkpoint | `ray-training/ray_training/entrypoints/train.py`, `loop.py` |
| Training placement | driver on the CPU pool (`cpu_pool` resource), trainer task `num_gpus` per preset | a run waits (PENDING) for the GPU instead of failing; the CPU driver never holds the card | docs/trainer-plan.md, `app/core/presets.py` |
| Ingest | Ray jobs (`ray_jobs`), `compute.backend` = `ray_dashboard` or `rayjob` | dataset imports run on the CPU worker group, off the API pods | `ray-jobs/`, `app/integrations/compute.py` |
| Serving | Ray Serve, two shared apps (`predictor-cpu`, `predictor-gpu`), multiplexed models, `max_ongoing_requests` | any number of deployments share replicas; a replica keeps its 8 most recent models loaded | `ray-serving/ray_serving/app.py`, `app/integrations/serve_config.py` |
| Serving scale | `autoscaling_config` with `target_ongoing_requests`, upscale and downscale delays | replicas follow queue depth; a long downscale delay keeps models warm through a lull | `serve_config.render`, knobs in `config/base.yaml` |
| Background predictions | core-api fans claimed predictions out to Serve concurrently (`predict.worker_concurrency`) | a burst of `Prefer: respond-async` calls lands on all replicas at once; Serve's own queue and autoscaling absorb it | `app/background.py` prediction worker |

## Designed, not built

1. **Batch inference job (`kind: predict_batch`).** For a large offline set (thousands of images) the per-request gateway is the
   wrong path. A new `BatchPredictSpec` beside `IngestSpec` and `TrainSpec` (same envelope, `report.json` result, GPU-seconds
   metering) runs `ray.data.read_binary_files` on a dataset version, then `map_batches` with an actor pool that loads the model once
   per actor (`concurrency` and `batch_size` from the preset), and writes predictions to the staging prefix. It reuses
   `ray_serving.model.load/predict`, so a batch result equals a live one. Needs: job kind, spec test, a route to start it, quota.
2. **Hyperparameter search (`kind: tune`).** Ray Tune with the ASHA scheduler over the bounds that already live in
   `task_presets.hyperparameters` (min, max, default). Each trial is the existing Ray Train run; `max_concurrent_trials` caps GPU use;
   the best trial registers as the model version. Deferred by doc 17 ("tuning deferred"), so this is a product decision first.
3. **Dynamic batching in Serve (`@serve.batch`).** Only worth it on the GPU app. It must batch per multiplexed model id (a batch
   cannot mix models), and ONNX Runtime inputs must be stacked per model signature. Measure latency and throughput on the GPU first.

## Rules of thumb

- Per-request, latency-bound work goes through Serve; offline, throughput-bound work goes through a Ray job (Data or Tune).
- Anything that holds the single GPU for long waits PENDING; do not add a second GPU consumer without a fractional-GPU plan
  (doc 07, "GPU memory and sharing").
- Local stack has no GPU: the `small` preset is made CPU-only by `scripts/steps/cpu-preset.sh`.
