"""What the platform knows about each supported task type, in one table: a new task type is one entry here (plus a
member of `TaskType` and, for a task not in projects.task_type's CHECK yet, a migration).

`TASKS` carries what differs between task types: the kind of data a task works on, one pretrained checkpoint per preset
for a task the trainer can run (pinned to a commit, docs/trainer-plan.md T6), and whether a deployment can serve its
models. Everything that asks "is it supported / tabular / trainable / servable" asks this module; `job_spec.TrainTask`
and the deployment error text derive from it. Report parsing, model registration and metering do not branch on the task.
"""
from collections.abc import Mapping
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from app.core.enums import TaskType, TrainPreset
from app.core.problem import Problem


class DataKind(StrEnum):
    IMAGES = "images"
    TABULAR = "tabular"


class TrainBaseModel(BaseModel):
    """A pretrained checkpoint on the Hugging Face Hub, pinned to a commit."""

    repo_id: str
    revision: str


class TaskInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    data_kind: DataKind
    base_models: Mapping[TrainPreset, TrainBaseModel] = {}  # empty: not trainable yet
    servable: bool = False  # the serving script in serve_client can serve its models


TASKS: Mapping[TaskType, TaskInfo] = {
    TaskType.IMAGE_CLASSIFICATION: TaskInfo(
        data_kind=DataKind.IMAGES,
        servable=True,
        base_models={
            TrainPreset.SMALL: TrainBaseModel(repo_id="facebook/convnext-tiny-224", revision="6166b7613034066690a621d8bf25ffdf181a34f0"),
            TrainPreset.MEDIUM: TrainBaseModel(repo_id="facebook/convnext-small-224", revision="e0df91dcc78dbc0f56be8fbecff706128aee7c28"),
            TrainPreset.LARGE: TrainBaseModel(repo_id="facebook/convnext-base-224", revision="8550c1da008c99b90c9c42f1d4e1cb650d7a8c05"),
        },
    ),
    TaskType.OBJECT_DETECTION: TaskInfo(
        data_kind=DataKind.IMAGES,
        base_models={
            TrainPreset.SMALL: TrainBaseModel(repo_id="PekingU/rtdetr_v2_r18vd", revision="5650961749fa93567c0d46fc7f43ea4f9e914107"),
            TrainPreset.MEDIUM: TrainBaseModel(repo_id="PekingU/rtdetr_v2_r34vd", revision="5d60d8fb5eb2a3ee5575bf200ecbce4e2cfa420c"),
            TrainPreset.LARGE: TrainBaseModel(repo_id="PekingU/rtdetr_v2_r50vd", revision="282494075698cab9faa1096ae26856890030c817"),
        },
    ),
    TaskType.TABULAR_CLASSIFICATION: TaskInfo(data_kind=DataKind.TABULAR),
    TaskType.TABULAR_REGRESSION: TaskInfo(data_kind=DataKind.TABULAR),
}


TRAINABLE: tuple[TaskType, ...] = tuple(t for t, i in TASKS.items() if i.base_models)  # job_spec.TrainTask is these
SERVABLE: tuple[TaskType, ...] = tuple(t for t, i in TASKS.items() if i.servable)


def info(task: TaskType) -> TaskInfo:
    return TASKS[task]


def supported(task: TaskType) -> bool:
    return task in TASKS


def is_tabular(task: TaskType) -> bool:
    return task in TASKS and TASKS[task].data_kind is DataKind.TABULAR


def trainable(task: TaskType) -> bool:
    return task in TASKS and bool(TASKS[task].base_models)


def servable(task: TaskType) -> bool:
    return task in TASKS and TASKS[task].servable


def base_model(task: TaskType, preset: TrainPreset) -> TrainBaseModel:
    return TASKS[task].base_models[preset]


def require_supported_task_type(task_type: TaskType) -> None:
    if not supported(task_type):
        raise Problem(422, "task_type_not_supported", f"task type '{task_type}' is not supported yet")
