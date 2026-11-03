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

from app.core.enums import JobPackageName, PackageStatus, SmokeStatus
from app.core.problem import Problem
from app.integrations.data_plane import job_catalog, job_package, job_submit, layout
from tests.support import TenantCtx


def test_the_zip_is_deterministic_and_named_by_its_hash():
    data, key = job_package.build()
    assert key == f"{layout.JOB_PACKAGE_ROOT}/ray-jobs-{hashlib.sha256(data).hexdigest()}.zip"
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert "package/ray_jobs/__init__.py" in names and "package/ray_jobs/entrypoints/upload_images.py" in names
    assert not any("__pycache__" in n or n.startswith("tests") for n in names)
    job_package.build.cache_clear()
    assert job_package.build() == (data, key)


def test_ray_makes_the_unpacked_zip_importable_and_the_entrypoint_runs_as_main(tmp_path):
    data, _ = job_package.build()
    zipfile.ZipFile(io.BytesIO(data)).extractall(tmp_path)  # what Ray does for a py_module: the directory holding ray_jobs/ goes on the path
    argv = shlex.split(job_submit.command(f"{job_catalog.ENTRYPOINTS}.{job_catalog.JobType.UPLOAD_CSV.value}", "{}"))
    argv[0] = sys.executable  # `python` on PATH may be another interpreter
    done = subprocess.run(argv, cwd=tmp_path, capture_output=True, text=True, env={"PYTHONPATH": str(tmp_path / job_package.ZIP_ROOT)})
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
    return {"name": name, "sha256": sha, "s3_key": key, "status": status, "published_by": actor, "updated_by": actor, "updated_at": datetime.now(UTC), "smoke_status": SmokeStatus.PENDING, "smoke_job_id": None}


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
    assert zipfile.ZipFile(io.BytesIO(data)).namelist() == ["package/ray_training/__init__.py"]


def _zip(*names: str) -> tuple[bytes, str]:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for n in names:
            zf.writestr(n, "")
    return buf.getvalue(), hashlib.sha256(buf.getvalue()).hexdigest()


class StagedS3:
    """A staging bucket holding `objects` ({key: bytes})."""

    def __init__(self, objects):
        self.objects = objects

    def _get(self, Key):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return self.objects[Key]

    def head_object(self, Bucket, Key):
        return {"ContentLength": len(self._get(Key))}

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self._get(Key))}


def _staged(registry, monkeypatch, *names, sha=None):
    data, real = _zip(*names)
    sha = sha or real
    key = job_package.package_key(job_package.PACKAGE_DIRS[JobPackageName.RAY_TRAINING], sha)
    monkeypatch.setattr(job_package.lakefs, "_s3", lambda secret, endpoint=None: StagedS3({key: data}))
    return key, real


def test_a_staged_package_is_read_back_and_registered_as_a_candidate(registry, monkeypatch):
    key, sha = _staged(registry, monkeypatch, "package/ray_training/__init__.py", "package/ray_training/spec.py")
    package = asyncio.run(job_package.register_staged(JobPackageName.RAY_TRAINING, key, "ci"))
    assert (package.sha256, package.s3_key, package.status, package.published_by) == (sha, key, PackageStatus.CANDIDATE, "ci")


@pytest.mark.parametrize("names", [
    ["package/ray_jobs/__init__.py"],  # another package's directory
    ["package/ray_training/../evil.py"], ["package/ray_training/data.bin"], ["ray_training/__init__.py"], ["setup.py"], []])
def test_a_staged_package_must_be_python_files_under_its_own_directory(registry, monkeypatch, names):
    key, _ = _staged(registry, monkeypatch, *names)
    with pytest.raises(Problem) as err:
        asyncio.run(job_package.register_staged(JobPackageName.RAY_TRAINING, key, "ci"))
    assert err.value.status_code == 422 and not registry.rows


def test_the_object_must_hash_to_the_sha_in_its_key(registry, monkeypatch):
    key, _ = _staged(registry, monkeypatch, "package/ray_training/__init__.py", sha="0" * 64)
    with pytest.raises(Problem) as err:
        asyncio.run(job_package.register_staged(JobPackageName.RAY_TRAINING, key, "ci"))
    assert err.value.status_code == 422 and not registry.rows


@pytest.mark.parametrize("key", [
    "jobs/ray-jobs-" + "a" * 64 + ".zip",  # another package's key
    "imports/t/d/i/upload/source", "jobs/ray-training-short.zip", "../jobs/ray-training-" + "a" * 64 + ".zip", ""])
def test_only_the_key_the_name_and_hash_give_is_accepted(registry, monkeypatch, key):
    monkeypatch.setattr(job_package.lakefs, "_s3", lambda secret, endpoint=None: StagedS3({}))
    with pytest.raises(Problem) as err:
        asyncio.run(job_package.register_staged(JobPackageName.RAY_TRAINING, key, "ci"))
    assert err.value.status_code == 422 and not registry.rows


def test_a_key_that_is_not_in_staging_is_refused(registry, monkeypatch):
    key, _ = _staged(registry, monkeypatch, "package/ray_training/__init__.py")
    monkeypatch.setattr(job_package.lakefs, "_s3", lambda secret, endpoint=None: StagedS3({}))
    with pytest.raises(Problem) as err:
        asyncio.run(job_package.register_staged(JobPackageName.RAY_TRAINING, key, "ci"))
    assert err.value.status_code == 422


def test_the_ci_build_script_makes_the_same_zip_as_core_api():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("build_job_package", Path(__file__).parents[4] / "scripts" / "build-job-package.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    for name, package_dir in job_package.PACKAGE_DIRS.items():
        assert script.build(script.DIRS[name]) == job_package.build(package_dir)[0], name


def _pending(job_id="smoke-x"):
    return job_package.JobPackage.model_validate(package_row("ray_jobs", "d" * 64, "jobs/x.zip", "ci") | {"smoke_job_id": job_id})


@pytest.fixture
def settled(monkeypatch):
    seen = []

    async def set_smoke(conn, name, sha, smoke):
        seen.append(smoke)
        return _pending().model_copy(update={"smoke_status": smoke})

    async def get_pool():
        return SimpleNamespace(acquire=lambda: TenantCtx(None))

    monkeypatch.setattr(job_package, "get_pool", get_pool)
    monkeypatch.setattr(job_package.job_packages, "set_smoke", set_smoke)
    return seen


@pytest.mark.parametrize("written,expected", [(b'{"ok": true}', SmokeStatus.PASSED), (b'{"ok": false}', SmokeStatus.FAILED)])
def test_a_written_smoke_result_settles_the_package(settled, monkeypatch, written, expected):
    monkeypatch.setattr(job_package, "_read_smoke", lambda key: written)
    assert asyncio.run(job_package.refresh_smoke(_pending())).smoke_status == expected and settled == [expected]


@pytest.mark.parametrize("ray,expected", [
    (job_package.compute.JobStatus.FAILED, SmokeStatus.FAILED), (job_package.compute.JobStatus.STOPPED, SmokeStatus.FAILED),
    (job_package.compute.JobStatus.RUNNING, None)])
def test_without_a_result_a_dead_job_fails_and_a_live_one_stays_pending(settled, monkeypatch, ray, expected):
    async def status(job_id):
        return ray

    monkeypatch.setattr(job_package, "_read_smoke", lambda key: None)
    monkeypatch.setattr(job_package.compute, "status", status)
    assert asyncio.run(job_package.refresh_smoke(_pending())).smoke_status == (expected or SmokeStatus.PENDING)
    assert settled == ([expected] if expected else [])
