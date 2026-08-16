# Project and user management

## Hierarchy
`Org (tenant) -> Project -> Dataset(s) -> Model(s) -> Deployment(s)`

A project is the unit a non-technical user actually works in — one "app" they're building (e.g., "defect detection on line 3"). Datasets, labeling tasks, models, and deployments all nest under a project.

## Requirements
- FR1: Create/rename/archive/delete project. Archiving stops billing for compute but retains data; delete is destructive after confirmation + grace period.
- FR2: Per-project role assignment, independent of org-level role (a user can be `project_viewer` on project A and `project_editor` on project B within the same org).
- FR3: Project-level audit log: who created/modified/deleted a dataset, model, or deployment, and when. Required for trust with paying customers even at small scale.
- FR4: Project dashboard surfaces: dataset count + last updated, active model versions, deployment status, month-to-date cost (train + inference).
- FR5: Team invitation by email, role pre-assigned at invite time, invite expires after 7 days.
- NFR1: Listing projects/datasets/models must paginate — do not assume small N once a tenant has been active for months.

## Data model sketch
```
organizations (id, name, zitadel_org_id, created_at)
projects (id, org_id, name, status, created_at)
project_members (project_id, user_id, role)
datasets (id, project_id, name, lakefs_repo, current_commit, created_at)
models (id, project_id, task_type, mlflow_registered_name)
model_versions (id, model_id, mlflow_version, dataset_commit, status, created_at)
deployments (id, model_version_id, status, endpoint_url, created_at)
```

## Open questions
- Do non-technical users need a "workspace" concept above project (grouping multiple projects visually) or is a flat project list under the org sufficient at launch? Default: flat list, revisit once a customer has 10+ projects.
