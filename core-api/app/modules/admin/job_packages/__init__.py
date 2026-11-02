"""The job packages of the platform: publish the bundled source as a candidate, promote it to stable, retire it."""
from app.modules.admin.job_packages.routes import router

__all__ = ["router"]
