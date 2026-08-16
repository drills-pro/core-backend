# UI — no-code builder

## Design constraint
Primary user does not understand train/test split, hyperparameters, encoders, or loss functions. Every screen must hide these by default with an "advanced" escape hatch for power users later — never require the term to complete a task.

## Core flows
1. **Dataset upload** — drag-drop files or point at existing URLs (see open question in 06 re: pre-staging). Platform infers task type from user selection (detect / segment / classify / forecast), not from data inspection.
2. **Labeling** — embedded Label Studio project per dataset, launched from within the platform UI (iframe or deep link with SSO passthrough — no separate login). Progress bar: "120/500 images labeled."
3. **Model builder** — drag-drop blocks that map to a generated Ludwig YAML (classification/tabular path) or Ultralytics config (detect/segment path). Blocks: input source → preprocessing presets (fast/accurate) → backbone choice (few named presets, not a library browse) → output.
4. **HuggingFace import** — search box hitting `huggingface_hub` search API server-side, user picks a model card, platform validates task-type compatibility before allowing import.
5. **Training run** — one button, "Train." No split ratio input; platform computes and stores its own split, shown read-only after the fact for transparency ("80% train / 20% test, seeded").
6. **Model versioning** — every completed run creates a version; UI shows a simple version list with accuracy/metric badge and a "promote to production" action, not raw MLflow run IDs.
7. **Deployment** — "Deploy" button produces an endpoint URL + example curl/code snippet. No K8s/Ray terminology exposed.
8. **Cost/usage dashboard** — plain-language: "This project cost $42 this month (training: $30, inference: $12)."

## Requirements
- FR1: Every async operation (upload, label export, training, deployment) shows live status without requiring page refresh (poll or websocket against the job-status API from 04).
- FR2: Error states must translate backend failures into actionable language ("Training failed: 40% of images could not be read — check the file list" not a stack trace).
- FR3: Drag-drop model builder must be able to serialize to and deserialize from the underlying YAML/config so advanced users can export/import configs directly.
- NFR1: Mobile-responsive is not a v1 requirement — this is a desktop workflow tool. Don't spend budget on it.

## Open questions
- Does the drag-drop builder need undo/redo and versioned "drafts" before a config is submitted for training, or is submit-only sufficient for v1? Lean toward submit-only initially — adds real complexity for unclear early value.
