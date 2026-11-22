"""Tenant self-service: read, rename, delete and restore the tenant you belong to."""
from app.modules.user.tenants.readers import (
    admin_detail,
    admin_list,
    admin_org_ids,
    admin_orgs,
    admin_platform_stats,
    get_org_ids,
    get_plan_tier,
    get_ref,
    slug_taken,
)
from app.modules.user.tenants.routes import router
from app.modules.user.tenants.schemas import AdminTenantCreated, AdminTenantFull, AdminTenantOrg, AdminTenantRow
from app.modules.user.tenants.transitions import ADMIN_SETTABLE_STATUSES, set_status
from app.modules.user.tenants.writers import insert_admin_tenant, insert_tenant, merge_settings

__all__ = [
    "ADMIN_SETTABLE_STATUSES",
    "AdminTenantCreated",
    "AdminTenantFull",
    "AdminTenantOrg",
    "AdminTenantRow",
    "admin_detail",
    "admin_list",
    "admin_org_ids",
    "admin_orgs",
    "admin_platform_stats",
    "get_org_ids",
    "get_plan_tier",
    "get_ref",
    "insert_admin_tenant",
    "insert_tenant",
    "merge_settings",
    "router",
    "set_status",
    "slug_taken",
]
