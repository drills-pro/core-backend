"""Ownership checks, run before every gateway operation touches lakeFS, S3 or MLflow.

Each check is one query as the calling tenant, with an explicit `tenant_id` match as well as RLS. A resource
that is missing, soft-deleted elsewhere or another tenant's is the same 404 (`not_found`), and nothing outside
Postgres has been called by then.

The tables belong to the bounded contexts above this layer, so the query for each resource is theirs: the
owning package registers it here when it is imported (`register`), and a resource nobody registered is refused
(the check fails closed).
"""
import uuid
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any

from app.core.problem import not_found
from app.core.tenant_conn import TenantConn


class Resource(StrEnum):
    DATASET = "dataset"
    IMPORT = "dataset_import"
    VERSION = "dataset_version"  # the check returns the version's lakeFS commit
    PROJECT = "project"
    TRAINING_RUN = "training_run"


# (conn, tenant_id, *ids) -> a value that is not None when the tenant owns the resource. ids are in the order of
# the gateway call: for an import (import_id, dataset_id), for a version (version_id, dataset_id), and so on.
Checker = Callable[..., Awaitable[Any]]

_checkers: dict[Resource, Checker] = {}


def register(resource: Resource, checker: Checker) -> None:
    _checkers[resource] = checker


async def _check(resource: Resource, tenant_id: str, *ids: str):
    try:
        tenant, args = uuid.UUID(tenant_id), [uuid.UUID(i) for i in ids]
    except ValueError:
        raise not_found() from None
    checker = _checkers.get(resource)
    if checker is None:
        raise RuntimeError(f"no ownership check is registered for {resource}")
    async with TenantConn(tenant_id) as conn:
        value = await checker(conn, tenant, *args)
    if value is None:
        raise not_found()
    return value


async def dataset(tenant_id: str, dataset_id: str) -> None:
    await _check(Resource.DATASET, tenant_id, dataset_id)


async def dataset_import(tenant_id: str, dataset_id: str, import_id: str) -> None:
    await _check(Resource.IMPORT, tenant_id, import_id, dataset_id)


async def version_commit(tenant_id: str, dataset_id: str, version_id: str) -> str:
    """The lakeFS commit of a version of this tenant's dataset."""
    return await _check(Resource.VERSION, tenant_id, version_id, dataset_id)


async def project(tenant_id: str, project_id: str) -> None:
    await _check(Resource.PROJECT, tenant_id, project_id)


async def training_run(tenant_id: str, project_id: str, run_id: str) -> None:
    await _check(Resource.TRAINING_RUN, tenant_id, run_id, project_id)
