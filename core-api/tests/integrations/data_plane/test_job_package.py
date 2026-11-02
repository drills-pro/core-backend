"""The ray_jobs zip: deterministic, content-addressed, and importable by the entrypoint bootstrap."""
import asyncio
import hashlib
import io
import shlex
import subprocess
import sys
import zipfile
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from app.core.enums import JobPackageName, PackageStatus
from app.core.problem import Problem
from app.integrations.data_plane import ingest, job_catalog, job_package, layout
from tests.support import TenantCtx


def test_the_zip_is_deterministic_and_named_by_its_hash():
    data, key = job_package.build()
    assert key == f"{layout.JOB_PACKAGE_ROOT}/ray-jobs-{hashlib.sha256(data).hexdigest()}.zip"
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert "ray_jobs/__init__.py" in names and "ray_jobs/entrypoints/upload_images.py" in names
    assert not any("__pycache__" in n or n.startswith("tests") for n in names)
    job_package.build.cache_clear()
    assert job_package.build() == (data, key)


def test_the_bootstrap_makes_the_zip_importable_and_runs_the_entrypoint_as_main(tmp_path):
    data, _ = job_package.build()
    archive = tmp_path / "pkg.zip"
    archive.write_bytes(data)
    argv = shlex.split(ingest.entrypoint(archive.as_uri(), job_catalog.JobType.UPLOAD_CSV, "{}"))
    argv[0] = sys.executable  # `python` on PATH may be another interpreter
    done = subprocess.run(argv, cwd=tmp_path, capture_output=True, text=True, env={})
    assert "ModuleNotFoundError" not in done.stderr and done.returncode != 0
    assert "job spec version None" in done.stderr  # reached main() of the entrypoint: the spec {} is refused


class S3:
    def __init__(self):
        self.calls = []

    def head_object(self, **kw):
        self.calls.append("head")
        raise ClientError({"Error": {"Code": "404"}}, "HeadObject")

    def put_object(self, **kw):
        self.calls.append(("put", kw["Key"]))

    def generate_presigned_url(self, op, Params, ExpiresIn):
        return f"http://ray-side/{Params['Key']}"


@pytest.fixture
def registry(monkeypatch):
    """A one-row job_packages table behind get_pool, and an S3 that has nothing staged."""
    s3 = S3()
    monkeypatch.setattr(job_package.lakefs, "_s3", lambda secret, endpoint=None: s3)
    monkeypatch.setattr(job_package, "_uploaded", set())
    rows = {}

    class Conn:
        async def execute(self, sql, name, sha, key, actor):
            rows.setdefault((name, sha), package_row(name, sha, key, actor))

        async def fetchrow(self, sql, *args):
            if sql.startswith("UPDATE"):
                name, sha, status, actor = args
                rows[(name, sha)] |= {"status": status, "updated_by": actor}
                return rows[(name, sha)]
            if "status" in sql:
                return next((r for r in rows.values() if r["status"] == PackageStatus.STABLE), None)
            return rows.get(args)

    async def get_pool():
        return SimpleNamespace(acquire=lambda: TenantCtx(Conn()))

    monkeypatch.setattr(job_package, "get_pool", get_pool)
    s3.rows = rows
    return s3


def package_row(name, sha, key, actor, status=PackageStatus.CANDIDATE):
    return {"name": name, "sha256": sha, "s3_key": key, "status": status, "published_by": actor, "updated_by": actor, "updated_at": datetime.now(UTC)}


def test_publish_stages_a_new_version_once_and_registers_a_candidate(registry):
    data, key = job_package.build()
    first = asyncio.run(job_package.publish(JobPackageName.RAY_JOBS, "ops"))
    asyncio.run(job_package.publish(JobPackageName.RAY_JOBS, "ops"))
    assert (first.sha256, first.s3_key, first.status) == (hashlib.sha256(data).hexdigest(), key, PackageStatus.CANDIDATE)
    assert registry.calls == ["head", ("put", key)]  # the second publish finds it in this process's memory


def test_a_run_gets_the_registered_key_whatever_its_status(registry):
    package = asyncio.run(job_package.publish(JobPackageName.RAY_JOBS, "ops"))
    assert asyncio.run(job_package.presigned_url(JobPackageName.RAY_JOBS, package.sha256)) == f"http://ray-side/{package.s3_key}"
    with pytest.raises(LookupError):
        asyncio.run(job_package.presigned_url(JobPackageName.RAY_JOBS, "0" * 64))


def test_prod_runs_only_a_promoted_package(registry, monkeypatch):
    monkeypatch.setattr(job_package.settings, "env", job_package.PROD_ENV)
    asyncio.run(job_package.publish(JobPackageName.RAY_JOBS, "ops"))
    with pytest.raises(Problem) as err:
        asyncio.run(job_package.resolve_stable(JobPackageName.RAY_JOBS))
    assert err.value.status_code == 503
    (key,) = registry.rows
    registry.rows[key]["status"] = PackageStatus.STABLE
    assert asyncio.run(job_package.resolve_stable(JobPackageName.RAY_JOBS)).sha256 == key[1]


def test_outside_prod_the_bundled_source_is_published_and_promoted(registry):
    package = asyncio.run(job_package.resolve_stable(JobPackageName.RAY_TRAINING))
    assert package.status == PackageStatus.STABLE and package.published_by == job_package.SELF_PUBLISHER


def test_another_package_is_zipped_the_same_way_under_its_own_name(tmp_path):
    package = tmp_path / "ray-training" / "ray_training"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    data, key = job_package.build(package)
    assert key == f"{layout.JOB_PACKAGE_ROOT}/ray-training-{hashlib.sha256(data).hexdigest()}.zip"
    assert zipfile.ZipFile(io.BytesIO(data)).namelist() == ["ray_training/__init__.py"]
