from pydantic import BaseModel, ConfigDict


class RegisterBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    s3_key: str  # in the staging bucket: jobs/<slug>-<sha256>.zip
