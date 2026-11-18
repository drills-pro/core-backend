"""Dataset creation and listing; imports and versioning follow in docs/datasets-plan.md."""
from app.integrations.data_plane import OwnedResource, register_ownership_check
from app.modules.user.datasets.readers import (
    VersionRef,
    VersionSource,
    get_version_ref,
    get_version_source,
    in_project,
    owns_dataset,
    owns_import,
    owns_version,
)
from app.modules.user.datasets.reconcile import expire_stale_imports, purge_deleted_datasets, reconcile_inflight_imports
from app.modules.user.datasets.routes import router, trash_router
from app.modules.user.projects import register_dataset_check

register_ownership_check(OwnedResource.DATASET, owns_dataset)
register_ownership_check(OwnedResource.IMPORT, owns_import)
register_ownership_check(OwnedResource.VERSION, owns_version)
register_dataset_check(in_project)

__all__ = [
    "VersionRef",
    "VersionSource",
    "expire_stale_imports",
    "get_version_ref",
    "get_version_source",
    "purge_deleted_datasets",
    "reconcile_inflight_imports",
    "router",
    "trash_router",
]
