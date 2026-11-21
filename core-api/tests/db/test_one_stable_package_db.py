"""One stable package per name on a real Postgres as core_app: promote retires the previous stable, the index refuses two."""
import json

import asyncpg
import pytest

from app.core import job_packages
from app.core.enums import JobPackageName, PackageStatus

INSERT_PACKAGE = (
    "INSERT INTO job_packages (name, sha256, s3_key, published_by, updated_by, requirements) VALUES ($1, $2, 'jobs/test.zip', 'ci', 'ci', '[]')"
)
NAME = JobPackageName.RAY_JOBS
ACTOR = "admin"


def sha(n: int) -> str:
    return f"{n:064x}"


async def prepared(conn) -> None:
    """The jsonb codec the app's pool sets, so the helpers can parse rows; then two candidates."""
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
    for n in (1, 2):
        await conn.execute(INSERT_PACKAGE, NAME, sha(n))


async def statuses(conn) -> dict[str, str]:
    return {r["sha256"]: r["status"] for r in await conn.fetch("SELECT sha256, status FROM job_packages WHERE name = $1", NAME)}


def test_promote_leaves_exactly_one_stable(two_tenants):
    async def scenario():
        async with two_tenants.app_role() as conn:
            await prepared(conn)
            await job_packages.promote(conn, NAME, sha(1), ACTOR)
            await job_packages.promote(conn, NAME, sha(2), ACTOR)
            assert await statuses(conn) == {sha(1): PackageStatus.RETIRED, sha(2): PackageStatus.STABLE}

    two_tenants.run(scenario())


def test_the_index_refuses_a_second_stable_row(two_tenants):
    async def scenario():
        async with two_tenants.app_role() as conn:
            await prepared(conn)
            await job_packages.set_status(conn, NAME, sha(1), PackageStatus.STABLE, ACTOR)
            with pytest.raises(asyncpg.UniqueViolationError):
                await job_packages.set_status(conn, NAME, sha(2), PackageStatus.STABLE, ACTOR)

    two_tenants.run(scenario())
