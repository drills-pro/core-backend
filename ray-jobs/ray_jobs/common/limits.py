"""The ceilings core-api sends in params, and the exception that reports one being crossed."""
from dataclasses import dataclass, fields

from ray_jobs.common.enums import Code


@dataclass
class Limits:
    """Same names as settings.DatasetLimits; extra keys in the params are ignored."""

    upload_max_bytes: int
    extracted_max_bytes: int
    extracted_max_ratio: int
    max_files: int
    max_archive_depth: int
    max_path_length: int
    image_max_dimension: int
    image_max_bytes: int
    csv_min_rows: int
    csv_max_rows: int
    csv_max_columns: int
    hf_max_repo_bytes: int
    job_timeout_seconds: int

    @classmethod
    def from_dict(cls, d: dict) -> "Limits":
        return cls(**{f.name: d[f.name] for f in fields(cls)})  # KeyError on a missing knob; extra keys ignored


class LimitExceeded(Exception):
    def __init__(self, code: Code, message: str):
        super().__init__(message)
        self.code, self.message = code, message
