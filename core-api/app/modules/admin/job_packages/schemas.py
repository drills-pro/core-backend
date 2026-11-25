from pydantic import BaseModel, ConfigDict


class TargetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    s3_key: str  # in the staging bucket: jobs/<ray-jobs|ray-training|ray-serving>-<sha256>.zip, or jobs/<slug>-<sha256>.zip for a task
