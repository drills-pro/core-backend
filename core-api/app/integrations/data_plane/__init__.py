"""The data-plane gateway: the only code that talks to lakeFS, the staging store and MLflow, and the only way in.

Code outside this package imports from `app.integrations.data_plane` alone, never a submodule
(tests/architecture enforces it).

Tenant operations take the caller's tenant id plus resource ids (dataset, import, version, project), check in
Postgres that the tenant owns them, and only then call the backend; a foreign id is a 404. The check runs on a
pooled connection of its own, so a caller must hold no pooled connection across a gateway call: holding one
while waiting for the gateway could exhaust the pool. Callers therefore wrap each call in short transactions.

Designed to be lifted into its own service when core-worker becomes a second caller. Staging layout: layout.py.
Import report schema: report.py; training report schema: train_report.py.
"""
from app.integrations.data_plane import lakefs
from app.integrations.data_plane.access import Resource as OwnedResource
from app.integrations.data_plane.access import register as register_ownership_check
from app.integrations.data_plane.data_kinds import LAYOUTS as DATA_KIND_LAYOUTS
from app.integrations.data_plane.data_kinds import ingest_job_type
from app.integrations.data_plane.ingest import submit_ingest
from app.integrations.data_plane.job_catalog import JobType
from app.integrations.data_plane.job_package import SERVING_PACKAGE_DIR
from app.integrations.data_plane.job_package import presigned_url as presigned_package_url
from app.integrations.data_plane.job_package import publish as publish_package
from app.integrations.data_plane.job_package import refresh_smoke as refresh_package_smoke
from app.integrations.data_plane.job_package import register_staged as register_staged_package
from app.integrations.data_plane.job_package import resolve_stable as resolve_stable_package
from app.integrations.data_plane.job_package import retry_smoke as retry_package_smoke
from app.integrations.data_plane.job_package import start_smoke as start_package_smoke
from app.integrations.data_plane.task_bundle import register_staged as register_staged_bundle
from app.integrations.data_plane.lakefs import (
    commit_staged,
    create_dataset_repo,
    delete_dataset_repo,
    presign_get,
    presign_gets,
    presign_staging_put,
    purge_staging,
    read_report,
    read_staged_sample,
    read_version_sample,
)
from app.integrations.data_plane.lakefs import health as lakefs_health
from app.integrations.data_plane.layout import (
    MODEL_KEY,
    MODEL_META_KEY,
    OBJECTS_PREFIX,
    PREDICTION_INPUT_KEY,
    PREDICTION_OUTPUT_KEY,
    SCHEMA_KEY,
    TOKENIZER_KEY,
    staging_prefix,
    train_prefix,
)
from app.integrations.data_plane.mlflow import (
    ModelStage,
    RunStatus,
    display_name,
    finish_run,
    open_model_file,
    publish_run_model,
    register_model_version,
    registered_model_name,
    run_model_url,
    start_run,
    transition_stage,
)
from app.integrations.data_plane.mlflow import artifact_url as mlflow_artifact_url
from app.integrations.data_plane.mlflow import health as mlflow_health
from app.integrations.data_plane.prediction_payloads import Payload as PredictionPayload
from app.integrations.data_plane.prediction_payloads import delete as delete_prediction_payloads
from app.integrations.data_plane.prediction_payloads import load as load_prediction_payload
from app.integrations.data_plane.prediction_payloads import store as store_prediction_payload
from app.integrations.data_plane.report import ImportReport, Issue, IssueCode, Outcome, Severity, Stats
from app.integrations.data_plane.train import prepare_train_data, purge_train_prefix, read_model_meta, read_train_report, submit_train
from app.integrations.data_plane.train_report import FailureCode, TrainOutcome, TrainReport

__all__ = [
    # tenant operations: ownership is checked first (OPERATIONS)
    "create_dataset_repo", "presign_staging_put", "presign_get", "presign_gets", "read_version_sample",
    "read_staged_sample", "read_report", "commit_staged", "purge_staging", "delete_dataset_repo", "submit_ingest",
    "prepare_train_data", "submit_train", "read_train_report", "purge_train_prefix", "read_model_meta", "start_run",
    "finish_run", "publish_run_model", "run_model_url", "open_model_file", "register_model_version", "transition_stage",
    # prediction payloads: keyed by the prediction row's own tenant, project and id (prediction_payloads.py)
    "PredictionPayload", "store_prediction_payload", "load_prediction_payload", "delete_prediction_payloads",
    # platform operations: no tenant (job packages, health)
    "publish_package", "register_staged_package", "refresh_package_smoke", "start_package_smoke", "retry_package_smoke",
    "resolve_stable_package", "register_staged_bundle", "presigned_package_url", "lakefs_health", "mlflow_health", "mlflow_artifact_url",
    # ownership checks, registered by the contexts that own the tables
    "OwnedResource", "register_ownership_check",
    # the staging and model layout, the job catalog
    "OBJECTS_PREFIX", "SCHEMA_KEY", "MODEL_KEY", "MODEL_META_KEY", "TOKENIZER_KEY", "PREDICTION_INPUT_KEY",
    "PREDICTION_OUTPUT_KEY", "staging_prefix", "train_prefix", "DATA_KIND_LAYOUTS", "ingest_job_type", "JobType",
    "SERVING_PACKAGE_DIR",
    # MLflow names and states
    "ModelStage", "RunStatus", "display_name", "registered_model_name",
    # the import and training reports the jobs write
    "ImportReport", "Issue", "IssueCode", "Outcome", "Severity", "Stats", "FailureCode", "TrainOutcome", "TrainReport",
]

# Every tenant operation, including the two only train.py calls; tests/integrations/data_plane/test_gateway.py checks
# that each one 404s a foreign tenant before any backend is reached.
OPERATIONS = (
    create_dataset_repo,
    presign_staging_put,
    presign_get,
    presign_gets,
    read_version_sample,
    lakefs.read_version_column,
    lakefs.presign_version_objects,
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
    start_run,
    finish_run,
    publish_run_model,
    run_model_url,
    open_model_file,
    register_model_version,
    transition_stage,
)
