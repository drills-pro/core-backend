import uuid

from pydantic import BaseModel, Field

from app.core.authz import TenantRole, TenantStatus
from app.core.schemas import TenantName


class TenantCreate(BaseModel):
    name: TenantName


class TenantCreated(BaseModel):
    id: uuid.UUID
    name: str
    role: TenantRole = TenantRole.OWNER


class TenantSummary(BaseModel):
    id: uuid.UUID
    name: str
    role: TenantRole
    status: TenantStatus
    capabilities: list[str] = Field(description="Tenant-level actions the role holds")


class Me(BaseModel):
    sub: str
    email: str | None
    email_verified: bool
    name: str | None
    tenants: list[TenantSummary]
