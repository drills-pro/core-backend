"""The data-plane gateway: the only code that talks to lakeFS, the staging store and MLflow.

Operations take the caller's tenant id plus resource ids (dataset, import, version, project), check in
Postgres that the tenant owns them, and only then call the backend; a foreign id is a 404. Designed to be
lifted into its own service when core-worker becomes a second caller. Staging layout: layout.py. Import
report schema: report.py.
"""
from app.integrations.data_plane import mlflow
from app.integrations.data_plane.ingest import submit_ingest
from app.integrations.data_plane.job_package import publish as publish_package  # noqa: F401 - used by the admin job-packages routes, not a tenant operation
from app.integrations.data_plane.job_package import register_staged as register_staged_package  # noqa: F401 - used by the admin job-packages routes, not a tenant operation
from app.integrations.data_plane.job_package import refresh_smoke as refresh_package_smoke  # noqa: F401 - used by the admin job-packages routes, not a tenant operation
from app.integrations.data_plane.job_package import start_smoke as start_package_smoke  # noqa: F401 - used by the admin job-packages routes, not a tenant operation
from app.integrations.data_plane.job_package import retry_smoke as retry_package_smoke  # noqa: F401 - used by the admin job-packages routes, not a tenant operation
from app.integrations.data_plane.job_package import resolve_stable as resolve_stable_package  # noqa: F401 - used by the training module, not a tenant operation
from app.integrations.data_plane.lakefs import (
    commit_staged,
    create_dataset_repo,
    delete_dataset_repo,
    presign_get,
    presign_gets,
    presign_version_objects,
    presign_staging_put,
    purge_staging,
    read_report,
    read_staged_sample,
    read_version_column,
    read_version_sample,
)
from app.integrations.data_plane.train import prepare_train_data, purge_train_prefix, read_model_meta, read_train_report, submit_train

OPERATIONS = (
    create_dataset_repo,
    presign_staging_put,
    presign_get,
    presign_gets,
    read_version_sample,
    read_version_column,
    presign_version_objects,
    read_staged_sample,
    read_report,
    commit_staged,
    purge_staging,
    delete_dataset_repo,
    submit_ingest,
    prepare_train_data,
    submit_train,
    read_train_report,
    purge_train_prefix,
    read_model_meta,
    mlflow.start_run,
    mlflow.finish_run,
    mlflow.publish_run_model,
    mlflow.run_model_url,
    mlflow.open_model_file,
    mlflow.register_model_version,
    mlflow.transition_stage,
)
