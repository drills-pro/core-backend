"""The job code the platform runs on Ray, one `job_packages` row per (name, content hash): where its zip is staged and
whether runs may use it (candidate -> stable -> retired). Platform data, written only through /v1/admin/job-packages.
"""
from datetime import datetime

from pydantic import BaseModel

from app.core.enums import JobPackageName, PackageStatus, SmokeStatus


SHA256_PATTERN = r"^[0-9a-f]{64}$"  # mirrors job_packages_sha256_check


class JobPackage(BaseModel):
    name: JobPackageName
    sha256: str
    s3_key: str
    status: PackageStatus
    published_by: str
    updated_by: str
    updated_at: datetime
    smoke_status: SmokeStatus
    smoke_job_id: str | None


_SELECT = "SELECT * FROM job_packages"
_INSERT = (
    "INSERT INTO job_packages (name, sha256, s3_key, published_by, updated_by) VALUES ($1, $2, $3, $4, $4) "
    "ON CONFLICT (name, sha256) DO NOTHING"
)
_SET_STATUS = "UPDATE job_packages SET status = $3, updated_by = $4, updated_at = now() WHERE name = $1 AND sha256 = $2 RETURNING *"


async def register(conn, name: JobPackageName, sha256: str, s3_key: str, actor: str) -> JobPackage:
    """A candidate for this content, or the row that already exists for it."""
    await conn.execute(_INSERT, name, sha256, s3_key, actor)
    return JobPackage.model_validate(dict(await conn.fetchrow(f"{_SELECT} WHERE name = $1 AND sha256 = $2", name, sha256)))


async def get(conn, name: JobPackageName, sha256: str) -> JobPackage | None:
    row = await conn.fetchrow(f"{_SELECT} WHERE name = $1 AND sha256 = $2", name, sha256)
    return None if row is None else JobPackage.model_validate(dict(row))


async def latest_stable(conn, name: JobPackageName) -> JobPackage | None:
    row = await conn.fetchrow(f"{_SELECT} WHERE name = $1 AND status = $2 ORDER BY updated_at DESC LIMIT 1", name, PackageStatus.STABLE)
    return None if row is None else JobPackage.model_validate(dict(row))


async def list_for(conn, name: JobPackageName) -> list[JobPackage]:
    return [JobPackage.model_validate(dict(r)) for r in await conn.fetch(f"{_SELECT} WHERE name = $1 ORDER BY updated_at DESC", name)]


async def set_status(conn, name: JobPackageName, sha256: str, status: PackageStatus, actor: str) -> JobPackage | None:
    row = await conn.fetchrow(_SET_STATUS, name, sha256, status, actor)
    return None if row is None else JobPackage.model_validate(dict(row))


async def start_smoke(conn, name: JobPackageName, sha256: str, job_id: str) -> None:
    await conn.execute("UPDATE job_packages SET smoke_job_id = $3 WHERE name = $1 AND sha256 = $2 AND smoke_job_id IS NULL", name, sha256, job_id)


async def set_smoke(conn, name: JobPackageName, sha256: str, smoke: SmokeStatus) -> JobPackage:
    row = await conn.fetchrow("UPDATE job_packages SET smoke_status = $3 WHERE name = $1 AND sha256 = $2 RETURNING *", name, sha256, smoke)
    return JobPackage.model_validate(dict(row))
