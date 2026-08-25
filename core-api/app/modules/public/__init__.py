"""Unauthenticated read-only view of public projects."""
from app.modules.public.routes import router

__all__ = ["router"]
