"""Customer-facing job events: the outbox rows `job_lifecycle.advance` writes, listed per project."""
from app.modules.user.job_events.routes import router

__all__ = ["router"]
