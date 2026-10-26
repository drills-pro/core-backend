# Datasets: scope and decisions for the first slice

Status: decided in a design interview, 2026-10-24, and implemented through step 10 (step 11, the compose walk-through, is open). Design sources: `../planning/` docs 03, 05, 06, 10, 12, 16, 17.
No production data exists, so migrations may reshape or drop old tables; pushed migrations stay frozen (add new ones).

Diagrams (relationships, import lifecycle, architecture) and known gaps: [datasets-architecture.md](datasets-architecture.md).

## Why this slice exists

The current `app/modules/user/datasets` came from the pre-rework codebase: Ultralytics folder layout, one shared lakeFS repo, archives unpacked inside the API process, mutable working branch with an explicit commit. Ultralytics is gone, so the data layer is rebuilt around imports and immutable versions.

## Decisions

| # | Topic | Decision |
|---|---|---|
| 1 | Task types | HF task slugs verbatim as the `TaskType` enum, mirrored by a DB `CHECK` and a drift test. Full image/tabular/text/audio taxonomy is listed; unsupported ones answer "not supported yet". Existing underscore values are renamed in a new migration. `legacy_untyped` and the shape gate are removed. |
| 2 | Entry points (supported now) | Image: `image-classification` (zip of `<class>/<image>`), `object-detection` (zip with images plus one COCO `annotations.json`). Tabular: `tabular-classification`, `tabular-regression` via CSV. Hugging Face import for image and tabular tasks. YOLO is dropped. |
| 3 | Where processing runs | Ray ingest job for every entry point (no bytes through the API). Designed to move to `core-worker` and the job spec later: the `dataset_imports` table is the contract, the job is a standalone entrypoint that takes an import id. |
| 4 | Credentials | The job writes only to a staging prefix, with no lakeFS credential. core-api validates and commits to lakeFS (doc 16, ADR-018). |
| 5 | Versions and merge | One successful import = one new immutable `dataset_versions` row (number, lakeFS commit, source import, stats). An import may carry `base_version_id` to merge on top of it. Merge rules are strict: exact duplicates (path and sha256) are skipped; COCO categories merge by name with ids re-numbered; CSV/Parquet schemas must be identical; any other conflict fails the import with a structured report and creates no version. No overwrite mode. |
| 6 | Hugging Face import | Any public repo id. Revision pinned to a commit sha and recorded. Both the card's `task_categories` and the feature schema must verify the project's task type. Columns auto-detected, user picks among valid candidates only. Data files only (parquet, csv, jsonl, image folders), never loading scripts or remote code. Size cap from config. Licence recorded, not enforced. No gated or private repos, no stored user token. |
| 7 | CSV | Target column declared on the import (required). Job infers column types, stores `schema.json`; target type must fit the task. Header required, row minimum, size cap, unique column names. |
| 8 | Canonical storage | Tabular: `data.parquet` plus `schema.json`. Images: individual lakeFS objects plus `index.parquet` (path, size, sha256, label or boxes, split). No WebDataset shards. |
| 9 | Splits | Nullable `split` column (`train`, `validation`, `test`). HF splits are preserved; an import may tag all rows with one split. Datasets never invent a split; the trainer applies a seeded split and records the seed. |
| 10 | Import lifecycle | `POST /datasets/{id}/imports` (upload returns a presigned PUT to staging; HF validates card and schema up front), `POST .../start`, then states `awaiting_upload` -> `queued` -> `running` -> `awaiting_review` -> `committing` -> `succeeded`; also `failed`, `cancelled`, `expired`, `discarded`. Review shows the report and a sample preview; the only actions are `commit` and `discard`. TTLs expire stale `awaiting_upload` and `awaiting_review` and purge staging. Idempotency-Key on create. |
| 11 | lakeFS layout | One repo per dataset (`ds-<dataset_id>`), versions are commits on `main`. Deleting a dataset deletes the repo and its storage namespace. |
| 12 | RBAC for lakeFS and MLflow | A gateway package inside core-api (service-shaped interface, typed operations taking only tenant and resource ids, never names). It is the only code importing the lakeFS or MLflow clients and secrets; an architecture test enforces it and a contract test calls every operation with a foreign tenant. Extract to a service when `core-worker` becomes the second caller. |
| 13 | Publish | Kept, re-pointed: `datasets.published_version_id` (nullable FK to `dataset_versions`) replaces `published` and `published_ref`; `list_public_project_datasets` is replaced in the same migration. Metadata and stats only, no file access. |
| 14 | Concurrency | Imports and jobs run in parallel (own staging prefix each). Commit takes a row lock on the `datasets` row; `dataset_versions(dataset_id, number)` is unique; `awaiting_review -> committing` is a compare-and-set so retries are idempotent. A commit whose base is no longer the latest version gets `409 stale_base` (checked at `start` and again under the lock). |
| 15 | Legacy modules | Delete `labeling`, `preprocessing`, Ultralytics/Ludwig templates and old HF-import code in `ray_client`. `training` keeps its tables and authz and API shape but data access is stubbed with `501 not_implemented` until the trainer slice. A migration drops unused tables and reshapes `dataset_imports`. |
| 16 | Limits | Static named knobs in `config/*.yaml` through `settings.py`: upload and extracted size (ratio and total), file count, archive depth, path length, image dimension and bytes, CSV rows and columns, HF repo size, job timeout, Ray CPU and memory. Fixed archive rules: no absolute paths, `..` or symlinks, ignore `__MACOSX`, image extension allowlist plus real header decode, folder label normalisation (case collisions are conflicts). Per-tenant cap on active imports (`429`). Import usage events (count, bytes) via `core/metrics/usage.py`. |
| 17 | Dataset deletion | Soft delete (`deleted_at`) with a 7-day retention, restorable inside it; after that a background sweep deletes the lakeFS repo and storage namespace, then the rows. Independent of the tenant and project windows. |
| 18 | Audit | Audit rows (doc 19 E1) for import created, committed, discarded, expired, publish and unpublish, dataset delete and restore. |
| 19 | Task type | One task type per project; a dataset takes its project's `task_type` and every import is checked against it. |
| 20 | Pagination | Cursor pagination (ADR-028, `core/pagination.py`) for datasets, versions and imports, replacing offset paging. |
| 21 | Preview and files | Served from `index.parquet` (or `data.parquet` for tabular) plus presigned GETs, not by listing lakeFS. |

## Deferred (look here later)

- Plan-based storage and import quotas (doc 14 entitlements); today only static limits and the per-tenant concurrency cap.
- Editing during review (exclude a class, drop flagged files, de-duplicate); review is accept or discard only.
- Automatic rebase when a base version goes stale; today it is `409 stale_base` and a re-import.
- Overwrite or permissive merge mode.
- HF text and audio import, and the validators for text and audio task types; the enum values already exist.
- Hugging Face image-folder repos (no card features, label from the directory); the ingest job takes parquet, csv and jsonl data files only.
- Segmentation and other image task types beyond classification and detection.
- WebDataset shards, node cache and prewarm (doc 16); sharding would be a derived re-packing job over `index.parquet`.
- Multipart upload for archives above the single-PUT limit (5 GB).
- Catalog admission for HF datasets (doc 17): licence allowlist, mirror, admin flow.
- Extracting the gateway into its own service, and moving ingest from Ray to `core-worker` and the job spec (`kind=ingest`).
- MLflow behind the same gateway (interface designed now, migration with the trainer slice).
- Anonymous or public file access for published datasets (needs the compliance design, doc 18).
- Rewiring `training` to `version_id` and new trainer templates.
- Per-import staging credentials (STS) or a per-tenant prefix policy: the `ingest` credential is bucket-wide today, so a compromised ingest job can read or forge another tenant's staging prefix. Needed before prod; `commit_staged` only checks file names and sizes.
- Hugging Face card and feature-schema check at create (decision 10): it runs in the ingest job (step 7), so a mismatch fails the import minutes after create instead of answering 422. Doing it in core-api needs outbound Hub access from the API (a `k8s` egress change).
- Tenant purge (30-day hard delete) across lakeFS repos; now just "delete the dataset repos".

## Implementation order

Each step ends green (`.venv/bin/python -m pytest tests -q`, architecture tests included) and is one commit or a few. Steps 1 to 4 are the foundation; 5 and 6 are the API; 7 (the ingest job) can run in parallel with 5 and 6 once the staging layout and report schema from step 3 are fixed. Existing helpers to reuse: `core/pagination.py` (`PageParams`, `keyset_clause`, `build_page`), `core/audit.record`, the project soft-delete pattern in `projects/service.py`, and `POLLERS` in `app/background.py` (leader-locked).

| # | Step | Done when (verify) |
|---|---|---|
| 0 | Baseline: run the test suite and `./scripts/migrations-check.sh` before touching anything. | Both pass; failures recorded so they are not blamed on this work. |
| 1 | **Task types.** `TaskType` becomes the HF slug enum (`constants.py` or `core/enums.py`, wherever it lives today); one `ImportSource` enum (`upload`, `huggingface`); supported-vs-reserved is a single frozenset. Migration renames existing values and sets the `CHECK`. Remove `legacy_untyped` and `_SHAPE_GATE`. | A test fails if the enum and the DB `CHECK` drift; an unsupported slug answers 422 "not supported yet"; no bare task-type literal remains (grep). |
| 2 | **Schema migration** (new file, timestamp from `date -u`). `dataset_versions(id, dataset_id, number, lakefs_commit, import_id, stats jsonb, created_at)`, unique `(dataset_id, number)`; reshape `dataset_imports` to the lifecycle states, `base_version_id`, `source`, `params`, `report`, `staging_key`, `expires_at`, `idempotency_key`; `datasets` gets `deleted_at` and `published_version_id`, loses `s3_prefix`, `published`, `published_ref`; replace `list_public_project_datasets`; drop labeling and preprocessing tables. Grant `core_app`, RLS enabled on new tables, status `CHECK` mirrored by an enum. | `migrations-check.sh` passes up and down; RLS test shows a foreign tenant sees no rows in `dataset_versions` and `dataset_imports`; enum/CHECK drift test covers import status. |
| 3 | **Gateway package** `app/integrations/data_plane/`: repo-per-dataset lakeFS operations (`create_dataset_repo`, `presign_staging_put`, `presign_get`, `commit_staged`, `delete_dataset_repo`), MLflow operations moved behind it, all taking tenant and resource ids only. Config knobs for every limit in decision 16 (`settings.py` plus `base.yaml`). Delete the old `lakefs_client` shared-repo code. Fix the staging layout and the `report` JSON schema here, since the ingest job depends on them. | Architecture test: only this package imports the lakeFS and MLflow clients and secret files; contract test calls every operation with a foreign tenant id and expects failure; settings test covers the new knobs. |
| 4 | **Remove legacy.** Delete `labeling`, `preprocessing`, the Ultralytics and Ludwig templates and old HF import in `ray_client.py` (keep generic submit, status, logs, stop); stub `training` data access with `501 not_implemented`; unregister the deleted routers and pollers. | Suite and architecture tests green (`test_composition_root_registers_every_module`); no import of the removed code; training routes still enforce authz and return 501. |
| 5 | **Datasets module split** into `constants.py`, `schemas.py` (pydantic), `service.py`, `routes.py`: create, list and get (cursor pagination), soft delete and restore, versions list. Audit rows via `audit.record`. | Route x role matrix covers every new route; foreign-tenant id is 404; pagination test (token, invalid token 400); audit rows asserted for delete and restore; a created dataset's task type comes from its project. |
| 6 | **Imports API**: create (idempotency key, per-tenant cap with 429, base version check), start, get, list (cursor), cancel, commit, discard. State machine with compare-and-set transitions, commit under `SELECT … FOR UPDATE` on the dataset row, `409 stale_base`, audit rows, import usage events. Ray and the gateway are faked in tests. | Tests: legal and illegal transitions; double commit returns the same version; two concurrent commits on one base give one success and one `stale_base`; discard purges staging; wrong task type for the project is 422; cap returns 429. |
| 7 | **Ingest job** (standalone entrypoint, no core-api imports): image classification zip, COCO detection, CSV (declared target, schema inference), HF import (card plus schema check, pinned revision, data files only). Writes `index.parquet` or `data.parquet`, `schema.json`, report, only to staging. Archive safety rules and limits from decision 16; strict merge against the base. | Pure-function unit tests on small fixtures for each source: valid, zip bomb, path traversal, symlink, corrupt image, wrong label case, COCO id clash, CSV schema mismatch, HF card/schema disagreement, merge conflict. Then one real run in compose against MinIO. |
| 8 | **Reconcilers and sweeps** in `POLLERS`: poll Ray for active imports (status, report, move to `awaiting_review`), expire stale `awaiting_upload` and `awaiting_review`, purge staging, delete datasets 7 days after `deleted_at` (repo, namespace, rows). | Tests with a fake clock: expiry, sweep skips datasets restored inside the window, sweep is idempotent when the repo is already gone. |
| 9 | **Preview and files** from the parquet index plus presigned GETs; review preview reads the staged index. | Test returns sample rows and URLs without any lakeFS listing call; foreign dataset is 404. |
| 10 | **Publish** as `published_version_id`: publish a chosen version or unpublish; public gallery function returns version number and stats only. | Test: publishing another dataset's version is 404; unpublish clears it; gallery output has no file access fields. |
| 11 | **End to end and docs.** Run the stack (`run-local`): create project, upload a zip, review, commit, merge a second zip, publish, delete, restore. Update `README`, the `run-local`, `config-and-secrets` and `auth-layer` skills where they mention datasets, and `docs/runtime-contract.md` if `prod.yaml` gains keys (then a matching `k8s` change is needed; do not push that repo without being asked). | The walk-through works against compose; docs match the code; `k8s-contract` checklist reviewed. |

Risks to watch: step 7 depends on how `ray_client` ships code to Ray today (check before writing the entrypoint); lakeFS repo creation per dataset adds latency to dataset create (create lazily on first import if it is slow); the staging prefix and Ray's S3 access need a MinIO and prod-bucket policy that only allows that prefix.
