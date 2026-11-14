"""Task bundles: register, smoke and promote the per-task training code (planning/25)."""
from app.modules.admin.task_bundles.routes import router
from app.modules.admin.task_bundles.service import auto_promote as auto_promote_bundles

__all__ = ["auto_promote_bundles", "router"]
