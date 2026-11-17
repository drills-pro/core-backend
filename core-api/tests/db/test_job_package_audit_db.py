"""The packages log's retention on a real Postgres as core_app: the newest rows of each package name stay."""
from datetime import UTC, datetime, timedelta

from app.core.enums import JobPackageName, PackageAuditAction

INSERT_PACKAGE = (
    "INSERT INTO job_packages (name, sha256, s3_key, published_by, updated_by, requirements) VALUES ($1, $2, 'jobs/test.zip', 'ci', 'ci', '[]')"
)
INSERT_AUDIT ="INSERT INTO job_package_audit (name, sha256, action, actor, created_at) VALUES ($1, $2, $3, $4, $5)"
OLDEST = datetime(2000, 1, 1, tzinfo=UTC)
KEEP = 2


def sha(n: int) -> str:
    return f"{n:064x}"


def test_the_purge_keeps_the_newest_rows_of_a_package_name(two_tenants):
    async def scenario():
        async with two_tenants.app_role() as conn:
            await conn.execute(INSERT_PACKAGE, JobPackageName.RAY_SERVING, sha(9))  # the audit rows reference this package
            for i, actor in enumerate(["a1", "a2", "a3", "a4"]):  # oldest first
                await conn.execute(INSERT_AUDIT, JobPackageName.RAY_SERVING, sha(9), PackageAuditAction.REGISTER, actor, OLDEST + timedelta(days=i))
            await conn.fetchval("SELECT purge_job_package_audit($1)", KEEP)
            kept = {r["actor"] for r in await conn.fetch("SELECT actor FROM job_package_audit WHERE name = $1", JobPackageName.RAY_SERVING)}
            assert {"a3", "a4"} <= kept
            assert not {"a1", "a2"} & kept

    two_tenants.run(scenario())
