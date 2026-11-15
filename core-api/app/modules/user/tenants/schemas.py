import uuid
from datetime import datetime

from pydantic import BaseModel

from app.core.authz import TenantRole, TenantStatus
from app.core.schemas import TenantName
from app.core.tenant_settings import TenantSettings


class TenantRename(BaseModel):
    name: TenantName


class TenantDelete(BaseModel):
    confirm_name: str  # must equal the tenant's current name: deleting is not one stray click


class TenantDetail(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    status: TenantStatus
    role: TenantRole
    plan_tier: str
    created_at: datetime
    deleted_at: datetime | None = None


class TenantAuditEntry(BaseModel):
    """One audit row of the tenant: tenant-level events have no project."""

    id: uuid.UUID
    project_id: uuid.UUID | None
    actor: str
    action: str  # free text: the actions of several modules share the table
    entity_type: str
    entity_id: str
    created_at: datetime


class TenantRef(BaseModel):
    """The identifying columns of a tenant, for other contexts."""

    id: uuid.UUID
    name: str
    slug: str


class TenantStatusChange(BaseModel):
    """What the admin status writer saw: the status before (unchanged when pending_delete) and the Zitadel org id."""

    previous: TenantStatus
    org_id: str


class AdminTenantCreated(BaseModel):
    """The row POST /v1/admin/tenants inserts."""

    id: uuid.UUID
    name: str
    zitadel_org_id: str | None
    plan_tier: str
    created_at: datetime


class AdminTenantRow(AdminTenantCreated):
    """One line of the back-office listing: the tenant plus its SECURITY DEFINER counters."""

    project_count: int
    member_count: int


class AdminTenantFull(AdminTenantRow):
    """The detail header: the listing row plus settings and the two extra counters."""

    settings: TenantSettings
    dataset_count: int
    model_count: int
