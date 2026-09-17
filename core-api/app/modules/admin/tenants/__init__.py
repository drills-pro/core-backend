"""Tenants as the platform operator sees them: all of them, at once,
and one of them in detail."""
from app.modules.admin.tenants.detail import router as detail_router
from app.modules.admin.tenants.routes import router

__all__ = ["detail_router", "router"]
