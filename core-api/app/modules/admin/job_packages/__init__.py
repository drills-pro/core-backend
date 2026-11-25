"""The job packages of the platform: CI's target call for a version, the poller that moves targets, and the reads."""
from app.modules.admin.job_packages.routes import router
from app.modules.admin.job_packages.service import promote_targets

__all__ = ["promote_targets", "router"]
