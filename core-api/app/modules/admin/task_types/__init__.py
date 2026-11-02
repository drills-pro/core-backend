"""The task types of the platform: what each one works on, which trainer trains it and whether it can be served."""
from app.modules.admin.task_types.routes import router

__all__ = ["router"]
