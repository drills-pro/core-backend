import uuid
from datetime import datetime

from pydantic import BaseModel

from app.core.enums import AdminAuditAction, AdminAuditEntity


class AdminAuditEntry(BaseModel):
    """A row of admin_audit."""

    id: uuid.UUID
    actor: str
    action: AdminAuditAction
    entity_type: AdminAuditEntity
    entity_id: str
    detail: dict
    created_at: datetime
