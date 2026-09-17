"""Tenants as the platform operator sees them: all of them, at once."""
from app.modules.admin.tenants.routes import router

__all__ = ["router"]
