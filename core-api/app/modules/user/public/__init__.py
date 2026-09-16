"""Unauthenticated read-only view of public projects."""
from app.modules.user.public.routes import router

__all__ = ["router"]
