"""job_packages.requirements on a real Postgres as core_app: stored through register, defaulting to none, always a JSON array."""
import json

import asyncpg
import pytest

from app.core import job_packages
from app.core.enums import JobPackageName
from tests.support.bundles import manifest

SHA = "c" * 64
PINS = ["numpy==2.1.0", "torch==2.11.0+cu128"]


def _with_codec(two_tenants):
    two_tenants.run(two_tenants.conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"))


def test_a_package_keeps_the_requirements_it_was_registered_with(two_tenants):
    _with_codec(two_tenants)

    async def register():
        async with two_tenants.app_role() as conn:
            plain = await job_packages.register(conn, JobPackageName.RAY_JOBS, SHA, "k", "ci")
            pinned = await job_packages.register(conn, JobPackageName.RAY_TRAINING, SHA, "k", "ci", PINS)
            bundle = await job_packages.register(conn, "task_x", SHA, "k", "ci", PINS, manifest())
            return plain.requirements, pinned.requirements, bundle.requirements

    assert two_tenants.run(register()) == ([], PINS, PINS)


def test_requirements_must_be_a_json_array(two_tenants):
    async def write():
        async with two_tenants.app_role() as conn:
            await conn.execute(
                "INSERT INTO job_packages (name, sha256, s3_key, published_by, updated_by, requirements) VALUES ('ray_jobs', $1, 'k', 'ci', 'ci', '{}'::jsonb)", SHA)

    with pytest.raises(asyncpg.CheckViolationError):
        two_tenants.run(write())
