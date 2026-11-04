import uuid
from datetime import datetime

from pydantic import BaseModel

from app.modules.user.usage.constants import UsageResource


class UsageEvent(BaseModel):
    id: int
    resource_type: UsageResource
    quantity: float  # numeric in Postgres; a JSON number, as before, not pydantic's Decimal-as-string
    unit: str
    occurred_at: datetime
    source_ref: str
    pricing_version: str


class UsageTotal(BaseModel):
    project_id: uuid.UUID
    resource_type: UsageResource
    unit: str
    total_quantity: float
