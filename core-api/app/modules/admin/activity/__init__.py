"""The platform-wide activity log: every tenant's audit trail at once."""
from app.modules.admin.activity.routes import router

__all__ = ["router"]
