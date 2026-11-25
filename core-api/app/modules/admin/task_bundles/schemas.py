from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.core.enums import TaskType
from app.core.job_packages import JobPackage
from app.core.presets import TaskPreset
from app.core.task_types import TaskInfo


class TaskBundle(JobPackage):
    """A task bundle's job_packages row (name task_<slug>, kind task, its manifest) with its slug. status stable = the one
    bundle new runs of the slug use; smoke_status is settled from the smoke job on every read."""

    slug: TaskType

    @classmethod
    def of(cls, package: JobPackage) -> "TaskBundle":
        return cls.model_validate({**package.model_dump(), "slug": package.manifest["slug"]})


class EnabledBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class CatalogTask(BaseModel):
    """One task the platform can run, as the task bundles derived it: the task_types row, its presets, the stable bundle that
    backs it (None for a row from before bundles) and whether a passing candidate is promoted without a person."""

    task: TaskInfo
    presets: list[TaskPreset]
    bundle: TaskBundle | None
    auto_promote: bool


class BundleSettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auto_promote: bool


class BundleSettings(BundleSettingsBody):
    slug: TaskType
    updated_by: str | None = None  # None: the slug has no row, so the defaults apply
    updated_at: datetime | None = None
