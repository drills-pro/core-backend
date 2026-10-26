"""The data-plane gateway: the only code that talks to lakeFS, the staging store and MLflow.

Operations take the caller's tenant id plus resource ids (dataset, import, version, project), check in
Postgres that the tenant owns them, and only then call the backend; a foreign id is a 404. Designed to be
lifted into its own service when core-worker becomes a second caller. Staging layout: layout.py. Import
report schema: report.py.
"""
from app.integrations.data_plane import mlflow
from app.integrations.data_plane.ingest import submit_ingest
from app.integrations.data_plane.lakefs import (
    commit_staged,
    create_dataset_repo,
    delete_dataset_repo,
    presign_get,
    presign_staging_put,
    purge_staging,
    read_report,
    read_staged_sample,
    read_version_sample,
)

OPERATIONS = (
    create_dataset_repo,
    presign_staging_put,
    presign_get,
    read_version_sample,
    read_staged_sample,
    read_report,
    commit_staged,
    purge_staging,
    delete_dataset_repo,
    submit_ingest,
    mlflow.start_run,
    mlflow.finish_run,
    mlflow.register_model_version,
    mlflow.transition_stage,
)
