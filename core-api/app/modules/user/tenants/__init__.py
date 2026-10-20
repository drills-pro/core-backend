"""Tenant self-service: read, rename, delete and restore the tenant you belong to."""
from app.modules.user.tenants.routes import router

__all__ = ["router"]
