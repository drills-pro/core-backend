from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.core.enums import PackageStatus


class StatusBody(BaseModel):
    """stable promotes a candidate whose smoke passed (the one it replaces is retired); retired takes a package out of use."""

    model_config = ConfigDict(extra="forbid")

    status: Literal[PackageStatus.STABLE, PackageStatus.RETIRED]


class TargetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    s3_key: str  # in the staging bucket: jobs/<ray-jobs|ray-training|ray-serving>-<sha256>.zip, or jobs/<slug>-<sha256>.zip for a task
