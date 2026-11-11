import uuid
from datetime import datetime

from pydantic import BaseModel


class JobEvent(BaseModel):
    """One state change of a job: `kind` is `<job>.<status>` (training.succeeded, import.failed, deployment.active)."""

    seq: int  # grows with commit order within a project; pass the last one back as `after` to read on
    kind: str
    entity_id: uuid.UUID
    status: str
    occurred_at: datetime
