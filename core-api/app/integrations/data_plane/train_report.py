"""The report a training run writes to `<run prefix>/report.json` (layout.py) and core-api stores in training_runs.report.
The trainer has no core-api imports, so the contract is the JSON Schema checked in next to this file
(train_report.schema.json, kept in sync by a test); core-api validates with TrainReport.

The job is untrusted input: core-api reads only what is named here, and takes the model files from the fixed
keys of layout.py, never from a key the report names. Unknown fields are ignored, so the trainer may add some.
"""
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

TRAIN_REPORT_SCHEMA_VERSION = 1
MAX_EPOCH_HISTORY = 500  # job_spec.MAX_EPOCHS

Metric = Annotated[float, Field(allow_inf_nan=False)]


class TrainOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class FailureCode(StrEnum):
    OOM = "oom"
    BAD_DATA = "bad_data"
    UNSUPPORTED = "unsupported"
    TIMEOUT = "timeout"
    INTERNAL = "internal"


class ConfusionMatrix(BaseModel):
    labels: list[str]
    matrix: list[list[int]]  # matrix[true][predicted], rows and columns in `labels` order


class TrainReport(BaseModel):
    schema_version: Literal[1] = TRAIN_REPORT_SCHEMA_VERSION
    status: TrainOutcome
    code: FailureCode | None = None  # set exactly when status is failed
    message: str | None = None
    metrics: dict[str, Metric] = {}  # scalars, logged to MLflow: classification top1/top5, detection map50/map50_95
    history: list[dict[str, Metric]] = Field(default=[], max_length=MAX_EPOCH_HISTORY)  # one entry per epoch, e.g. {"epoch": 1, "loss": 0.7}
    classes: list[str] = []  # the model's class names in output order; serving names its answers with them
    confusion_matrix: ConfusionMatrix | None = None  # classification only

    @model_validator(mode="after")
    def _code_iff_failed(self) -> "TrainReport":
        if (self.status is TrainOutcome.FAILED) != (self.code is not None):
            raise ValueError("`code` is set exactly when `status` is failed")
        return self


if __name__ == "__main__":  # regenerate the checked-in schema: python -m app.integrations.data_plane.train_report
    import json
    from pathlib import Path

    Path(__file__).with_name("train_report.schema.json").write_text(json.dumps(TrainReport.model_json_schema(), indent=2) + "\n")
