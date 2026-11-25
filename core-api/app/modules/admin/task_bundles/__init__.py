"""Task bundles: what a bundle writes when it becomes its task's target, and the task's reads and switches (planning/25)."""
from app.modules.admin.task_bundles.routes import router
from app.modules.admin.task_bundles.service import auto_promote_allowed, promote as promote_bundle, register as register_bundle

__all__ = ["auto_promote_allowed", "promote_bundle", "register_bundle", "router"]
