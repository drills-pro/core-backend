# Datasets: relationships, lifecycle and architecture

How projects and datasets relate, what a dataset and an import go through, and which components do the work.
Decisions and scope are in [datasets-plan.md](datasets-plan.md); the cluster side is in
[runtime-contract.md](runtime-contract.md). The state machine in code is `TRANSITIONS` in
`core-api/app/modules/user/datasets/constants.py`; the staging layout is the docstring of
`core-api/app/integrations/data_plane/layout.py`.

## 1. Project and dataset

```mermaid
erDiagram
    TENANT ||--o{ PROJECT : owns
    PROJECT ||--o{ DATASET : "has (task_type lives on the project)"
    DATASET ||--o{ DATASET_IMPORT : "ingest attempts"
    DATASET ||--o{ DATASET_VERSION : "immutable, numbered 1..n"
    DATASET_IMPORT ||--o| DATASET_VERSION : "commit creates exactly one"
    DATASET_IMPORT }o--o| DATASET_VERSION : "base_version_id (merge on top of)"
    DATASET }o--o| DATASET_VERSION : "published_version_id"
    PROJECT ||--o{ TRAINING_RUN : trains
    TRAINING_RUN }o--o| DATASET : "dataset / test dataset"
    PROJECT {
        enum task_type "image-classification, object-detection, tabular-*"
        enum visibility
        enum status
        ts deleted_at
    }
    DATASET {
        string name
        ts deleted_at "soft delete, 7 day retention"
        uuid published_version_id
    }
    DATASET_IMPORT {
        enum source "upload | huggingface"
        enum status "10 states"
        jsonb params
        jsonb report
        string idempotency_key
        ts expires_at "TTL, commit lease or job deadline"
    }
    DATASET_VERSION {
        int number
        string lakefs_commit
        jsonb stats
    }
```

A dataset has no task type of its own: it reads the project's, so every dataset in a project holds the same
kind of data. Every table carries `tenant_id` and has row-level security.

## 2. Product lifecycle

```mermaid
flowchart TD
    subgraph DS["Dataset"]
        A[Create dataset in project] --> B[Active]
        B -->|"DELETE (soft)"| C["Pending deletion<br/>running imports cancelled"]
        C -->|":restore within 7 days"| B
        C -->|"7 days pass: purge sweep"| D["Gone<br/>lakeFS repo + rows removed"]
    end
    subgraph VER["Versions"]
        B --> E["Import, then Review, then Commit"]
        E --> F["Version N (immutable)"]
        F -->|"PUT /published"| G["Published version<br/>shows in public gallery, stats only"]
        G -->|"DELETE /published"| F
        F --> H["Next import must set base_version_id = latest<br/>(no overwrite mode)"]
        H --> E
    end
```

### Import state machine

```mermaid
stateDiagram-v2
    [*] --> awaiting_upload: POST /imports (Idempotency-Key)
    awaiting_upload --> queued: start
    awaiting_upload --> expired: 24h TTL
    awaiting_upload --> cancelled: cancel
    queued --> awaiting_upload: Ray submit failed (retry)
    queued --> running: Ray job started
    queued --> failed: deadline
    queued --> cancelled: cancel
    running --> awaiting_review: job ok + report
    running --> failed: job error or timeout
    running --> cancelled: cancel
    awaiting_review --> committing: commit
    awaiting_review --> discarded: discard
    awaiting_review --> expired: 7 day TTL
    committing --> succeeded: version created
    committing --> awaiting_review: lease lapsed (process died)
    succeeded --> [*]
    failed --> [*]
    cancelled --> [*]
    expired --> [*]
    discarded --> [*]
```

Every arrow is a compare-and-set (`UPDATE ... WHERE status = <from>`), so a retry or a race gets 409 instead of
acting twice. Reusing an `Idempotency-Key` with a different request body is also a 409
(`idempotency_key_reused`).

## 3. Technical architecture

```mermaid
flowchart LR
    UI[Browser / BFF] -->|"JWT"| API
    subgraph API["core-api (FastAPI)"]
        R["routes.py<br/>HTTP only"] --> S["service / import_service<br/>preview_service"]
        S -->|"TenantConn (RLS)"| PG[("Postgres<br/>projects, datasets,<br/>imports, versions")]
        S --> GW["data_plane gateway<br/>access.py ownership check first"]
        BG["background sweeps<br/>reconcile 10s, expire 60s, purge 1h"] --> S
    end
    GW -->|"presigned PUT"| STG[("Staging bucket<br/>imports/tenant/dataset/import/")]
    UI -.->|"direct upload"| STG
    GW -->|"submit job + base-version URLs"| RAY["Ray<br/>ingest.py job"]
    RAY -->|"read upload, write out/ + report.json"| STG
    RAY -.->|"source=huggingface"| HF[Hugging Face Hub]
    GW -->|"commit: import out/ as one commit"| LFS[("lakeFS<br/>one repo per dataset")]
    LFS -->|"reads objects directly"| STG
    GW -->|"presigned GET at a version's commit"| UI
    GW --> MLF["MLflow<br/>tenant + project namespaced"]
    PG -.->|"SECURITY DEFINER lists"| PUB["/v1/public/* gallery<br/>(unauthenticated, no file access)"]
```

### Import flow end to end

```mermaid
sequenceDiagram
    participant U as User
    participant A as core-api
    participant P as Postgres
    participant S as Staging (S3)
    participant R as Ray ingest job
    participant L as lakeFS
    U->>A: POST /imports (source, params, base_version?)
    A->>P: insert import (cap check, advisory lock)
    A-->>U: 201 + presigned upload_url
    U->>S: PUT archive or CSV
    U->>A: POST /start
    A->>P: CAS awaiting_upload to queued
    A->>L: create repo (idempotent)
    A->>R: submit job (staging cred + base index URL)
    loop every 10s
        A->>R: poll status
    end
    R->>S: write out/ (index.parquet, objects/) + report.json
    A->>P: CAS to awaiting_review, store report
    U->>A: GET /imports/id/preview (staged sample)
    U->>A: POST /commit
    A->>P: CAS to committing (lease)
    A->>L: import out/ as one commit (idempotent by import id)
    A->>P: insert dataset_version N, CAS to succeeded
    A->>S: purge staging
```

## Design points

- The data-plane gateway is the only code that touches lakeFS, S3 or MLflow, and it checks ownership in Postgres
  first. A foreign id is a 404 before anything outside the database is called.
- File bytes never pass through core-api. Uploads go to staging by presigned PUT, and lakeFS reads staging itself
  on commit.
- The ingest job holds a staging credential and no lakeFS credential.
- The lakeFS commit carries the import id as metadata, so retrying a failed commit returns the commit already made.
- A commit holds a lease (`expires_at` while `committing`); a lapsed lease goes back to review via the expiry sweep
  or the next import's claim.

## Known gaps

- The ingest job's staging credential covers the whole staging bucket, not just its import's prefix. The job parses
  untrusted archives and images, so a parser exploit could read or overwrite other tenants' staged data. Fix:
  per-import STS credentials or presigned URLs for the single prefix. The credential also appears in Ray's
  `runtime_env`.
- Training creation (`POST /training-runs`) returns 501 until the trainer slice rewires it to dataset version ids
  (see Deferred in [datasets-plan.md](datasets-plan.md)).
