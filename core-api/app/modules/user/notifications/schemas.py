from pydantic import BaseModel, Field

from app.modules.user.job_events import JobEvent


class Notifications(BaseModel):
    unread: int  # every event after the caller's cursor
    items: list[JobEvent]  # the newest of them, newest first


class MarkRead(BaseModel):
    seq: int = Field(ge=0)  # the newest `seq` the caller has seen
