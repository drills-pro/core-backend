"""The dataset-import rows that follow a Ray ingest job, as job_lifecycle sees them. Shared by the request service
(cas) and the reconciler, which otherwise would have to import each other."""
from app.integrations import job_lifecycle
from app.modules.user.datasets.constants import TRANSITIONS, ImportStatus

RUNNING_STATUSES = (ImportStatus.QUEUED, ImportStatus.RUNNING)

IMPORTS = job_lifecycle.JobTable(
    name="import",
    table="dataset_imports",
    tenants_fn="inflight_import_tenant_ids",
    inflight=RUNNING_STATUSES,
    transitions=TRANSITIONS,
    columns=frozenset({"expires_at", "report"}),
    deadline=lambda row: row["expires_at"],  # the job deadline while the import is queued or running
)
