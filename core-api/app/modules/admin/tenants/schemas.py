"""What the back-office tenant routes answer. Rows of a SQL function mirror its RETURNS TABLE
(tests/architecture/test_admin_shapes.py fails when the two drift); Zitadel adds `status` and `owner`."""
import uuid
from datetime import datetime

from pydantic import BaseModel

from app.core.authz import TenantStatus
from app.core.enums import IdentityStatus
from app.core.tenant_settings import TenantSettings
from app.integrations.zitadel import OrgOwner, OrgUser
from app.modules.user.tenants import AdminTenantCreated, AdminTenantFull, AdminTenantRow


class TenantListItem(AdminTenantRow):
    status: IdentityStatus
    owner: OrgOwner | None


class TenantPage(BaseModel):
    rows: list[TenantListItem]
    total: int  # the size of the filtered set
    status_counts: dict[IdentityStatus, int]  # the filtered set by status, before the status filter


class CreatedTenant(AdminTenantCreated):
    status: IdentityStatus


class TenantDetail(AdminTenantFull):
    status: IdentityStatus
    owner: OrgOwner | None


class TenantsSummary(BaseModel):
    organizations: int
    organizations_new_30d: int
    projects: int
    projects_new_30d: int
    members: int
    status_counts: dict[IdentityStatus, int]


class TenantProject(BaseModel):
    """A row of admin_tenant_projects()."""

    id: uuid.UUID
    name: str
    status: str
    task_type: str | None
    visibility: str
    created_at: datetime
    dataset_count: int
    model_count: int
    model_name: str | None
    model_version: str | None


class TenantMember(OrgUser):
    """A row of admin_tenant_members(), with the person Zitadel knows behind it."""

    user_id: str
    roles: list[str]
    projects: list[str]
    project_count: int
    joined_at: datetime | None


class TenantDataset(BaseModel):
    """A row of admin_tenant_datasets()."""

    id: uuid.UUID
    name: str
    project_id: uuid.UUID
    project_name: str
    import_status: str | None
    created_at: datetime


class TenantSettingsOut(BaseModel):
    settings: TenantSettings


class TenantStatusOut(BaseModel):
    status: TenantStatus
