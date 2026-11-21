from pydantic import BaseModel, ConfigDict

from app.core.enums import PackageStatus


class StatusBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: PackageStatus


class RegisterBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    s3_key: str  # in the staging bucket: jobs/<ray-jobs|ray-training>-<sha256>.zip
