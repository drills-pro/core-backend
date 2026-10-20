import uuid
from datetime import datetime

from pydantic import BaseModel

from app.core.authz import TenantRole, TenantStatus
from app.core.schemas import TenantName


class TenantRename(BaseModel):
    name: TenantName


class TenantDelete(BaseModel):
    confirm_name: str  # must equal the tenant's current name: deleting is not one stray click


class TenantDetail(BaseModel):
    id: uuid.UUID
    name: str
    status: TenantStatus
    role: TenantRole
    plan_tier: str
    created_at: datetime
    deleted_at: datetime | None = None
