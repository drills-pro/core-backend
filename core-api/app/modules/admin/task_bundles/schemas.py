from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.core.enums import PackageStatus, TaskType


class RegisterBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    s3_key: str  # in the staging bucket: jobs/<slug>-<sha256>.zip


class BundleStatusBody(BaseModel):
    """stable promotes the bundle (and rewrites the task's row and presets from its manifest); retired takes it out of use."""

    model_config = ConfigDict(extra="forbid")

    status: Literal[PackageStatus.STABLE, PackageStatus.RETIRED]


class BundleSettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auto_promote: bool


class BundleSettings(BundleSettingsBody):
    slug: TaskType
    updated_by: str | None = None  # None: the slug has no row, so the defaults apply
    updated_at: datetime | None = None
