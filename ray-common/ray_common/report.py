"""What a train and a predict_batch report.json have in common: the version, the dataset the run used, the status words."""
from ray_common.constants import REPORT_SCHEMA_VERSION
from ray_common.enums import Status


def dataset_ref(version_id: str | None, lakefs_commit: str | None) -> dict:
    return {"version_id": version_id, "lakefs_commit": lakefs_commit}


def envelope(status: Status, code: str | None = None, message: str | None = None, **fields) -> dict:
    """A report: `fields` (the package's own keys) plus schema_version, status, code and message."""
    return {"schema_version": REPORT_SCHEMA_VERSION, **fields, "status": status, "code": code, "message": message}
