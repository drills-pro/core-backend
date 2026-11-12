import uuid

from pydantic import BaseModel, Field

from app.core.authz import TenantRole, TenantStatus
from app.core.schemas import TenantName, TenantSlug


class TenantCreate(BaseModel):
    name: TenantName
    slug: TenantSlug | None = None  # omitted: derived from the name


class TenantCreated(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    role: TenantRole = TenantRole.OWNER


class TenantSummary(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    role: TenantRole
    status: TenantStatus
    capabilities: list[str] = Field(description="Tenant-level actions the role holds")


class EmailVerificationStatus(BaseModel):
    verified: bool = Field(description="Zitadel's current answer, which may be newer than the caller's token")


class Me(BaseModel):
    sub: str
    email: str | None
    email_verified: bool
    name: str | None
    tenants: list[TenantSummary]
