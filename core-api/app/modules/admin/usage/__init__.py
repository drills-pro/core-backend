"""The platform-wide usage ledger: every tenant's usage_events at once."""
from app.modules.admin.usage.routes import router

__all__ = ["router"]
