"""Rows of admin_activity() and admin_activity_stats(); each mirrors its function's RETURNS TABLE
(tests/architecture/test_admin_shapes.py), and Zitadel adds `actor_name`."""
import uuid
from datetime import datetime

from pydantic import BaseModel


class ActivityItem(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    tenant_name: str | None
    project_id: uuid.UUID | None
    project_name: str | None
    actor: str
    action: str
    entity_type: str
    entity_id: str | None
    created_at: datetime
    actor_name: str | None  # None when Zitadel does not answer: the dashboard shows the bare subject id


class ActivityStats(BaseModel):
    events_today: int
    tenants_today: int
