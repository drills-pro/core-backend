"""The job-package lifecycle through its public functions: the zip, publish and register, the smoke, resolving the stable one.
The staging bucket and the job_packages tables are doubles (tests/support/packages.py); Ray is `job_submit` and `compute`."""
import asyncio
import hashlib
import io
import json
import re
import shlex
import subprocess
import sys
import zipfile

import httpx
import pytest
from botocore.exceptions import ClientError

from app.core.enums import JobPackageName, PackageAuditAction, PackageStatus, SmokeStatus
from app.core.job_packages import JobPackage
from app.core.problem import Problem
from app.core.requirements import parse_lock
from app.integrations.data_plane import job_catalog, job_package, job_submit, layout, package_store, package_zip
from tests.support.packages import FakeS3, PackageDb, client_error, package_row

SHA = "d" * 64
FIRST_SMOKE_JOB = f"smoke-ray-jobs-{SHA[:12]}"  # the id of the first smoke job of the ray_jobs package SHA


def test_the_zip_is_deterministic_and_named_by_its_hash():
    data, key = package_zip.build()
    assert key == f"{layout.JOB_PACKAGE_ROOT}/ray-jobs-{hashlib.sha256(data).hexdigest()}.zip"
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert "package/ray_jobs/__init__.py" in names and "package/ray_jobs/entrypoints/upload_images.py" in names
    assert "package/ray_common/__init__.py" in names and "package/ray_common/spec.py" in names
    assert not any("__pycache__" in n or n.startswith("tests") for n in names)
    package_zip.build.cache_clear()
    assert package_zip.build() == (data, key)


def test_ray_makes_the_unpacked_zip_importable_and_the_entrypoint_runs_as_main(tmp_path):
    data, _ = package_zip.build()
    zipfile.ZipFile(io.BytesIO(data)).extractall(tmp_path)  # what Ray does for a py_module: the directory holding ray_jobs/ goes on the path
    root = tmp_path / package_zip.ZIP_ROOT
    argv = shlex.split(job_submit.command(f"{job_catalog.ENTRYPOINTS}.{job_catalog.JobType.UPLOAD_CSV.value}", "{}"))
    argv[0] = sys.executable  # `python` on PATH may be another interpreter
    done = subprocess.run(argv, cwd=tmp_path, capture_output=True, text=True, env={"PYTHONPATH": str(root)})
    assert "ModuleNotFoundError" not in done.stderr and done.returncode != 0
    assert "job spec version None" in done.stderr  # reached main() of the entrypoint: the spec {} is refused


@pytest.mark.parametrize("name, module", [
    (JobPackageName.RAY_JOBS, "ray_jobs.spec"),
    (JobPackageName.RAY_TRAINING, "ray_core.spec"),
    (JobPackageName.RAY_SERVING, "ray_serving.batch_spec"),
])
def test_every_unpacked_zip_imports_the_shared_kernel(tmp_path, name, module):
    data, _ = package_zip.build(package_zip.PACKAGE_DIRS[name])
    zipfile.ZipFile(io.BytesIO(data)).extractall(tmp_path)
    done = subprocess.run([sys.executable, "-c", f"import ray_common, {module}"], cwd=tmp_path,
                          capture_output=True, text=True, env={"PYTHONPATH": str(tmp_path / package_zip.ZIP_ROOT)})
    assert done.returncode == 0, done.stderr
    assert "ModuleNotFoundError" not in done.stderr


@pytest.fixture
def s3(monkeypatch):
    """An empty staging bucket behind the S3 client."""
    bucket = FakeS3()
    monkeypatch.setattr(job_package.lakefs, "_s3", lambda secret, endpoint=None: bucket)
    monkeypatch.setattr(package_store, "_uploaded", set())  # the once-per-process memory of what is already staged
    return bucket


@pytest.fixture
def db(monkeypatch):
    """An empty job_packages table (and its audit log) behind get_pool."""
    tables = PackageDb()
    monkeypatch.setattr(job_package, "get_pool", tables.pool())
    return tables


def run(coro):
    return asyncio.run(coro)


def test_publish_stages_a_new_version_once_and_registers_a_candidate(s3, db):
    data, key = package_zip.build()
    sha = hashlib.sha256(data).hexdigest()
    first = run(job_package.publish(JobPackageName.RAY_JOBS, "ops"))
    run(job_package.publish(JobPackageName.RAY_JOBS, "ops"))
    assert (first.sha256, first.s3_key, first.status) == (sha, key, PackageStatus.CANDIDATE)
    assert (s3.heads, s3.puts) == ([key], [key])  # the second publish finds it in this process's memory
    assert db.audit == [(JobPackageName.RAY_JOBS, sha, PackageAuditAction.REGISTER, "ops")]  # one row: a known package is not registered again


def test_a_publish_the_bucket_cannot_answer_registers_nothing(s3, db):
    s3.head = client_error("403")
    with pytest.raises(ClientError):
        run(job_package.publish(JobPackageName.RAY_JOBS, "ops"))
    assert s3.puts == [] and db.rows == {}


def test_a_run_gets_the_registered_key_whatever_its_status(s3, db):
    package = run(job_package.publish(JobPackageName.RAY_JOBS, "ops"))
    assert run(job_package.presigned_url(JobPackageName.RAY_JOBS, package.sha256)) == f"http://ray-side/{package.s3_key}"
    with pytest.raises(LookupError):
        run(job_package.presigned_url(JobPackageName.RAY_JOBS, "0" * 64))


def test_prod_runs_only_a_promoted_package(s3, db, monkeypatch):
    monkeypatch.setattr(job_package.settings, "env", job_package.PROD_ENV)
    run(job_package.publish(JobPackageName.RAY_JOBS, "ops"))
    with pytest.raises(Problem) as err:
        run(job_package.resolve_stable(JobPackageName.RAY_JOBS))
    assert err.value.status_code == 503
    (key,) = db.rows
    db.rows[key]["status"] = PackageStatus.STABLE
    assert run(job_package.resolve_stable(JobPackageName.RAY_JOBS)).sha256 == key[1]


def test_outside_prod_the_bundled_source_is_published_and_promoted_with_an_audit_trail(s3, db):
    package = run(job_package.resolve_stable(JobPackageName.RAY_TRAINING))
    name = JobPackageName.RAY_TRAINING
    assert package.status == PackageStatus.STABLE and package.published_by == job_package.SELF_PUBLISHER
    assert db.audit == [(name, package.sha256, PackageAuditAction.REGISTER, job_package.SELF_PUBLISHER),
                        (name, package.sha256, PackageAuditAction.PROMOTE, job_package.SELF_PUBLISHER)]
    assert run(job_package.resolve_stable(name)).sha256 == package.sha256 and len(db.audit) == 2  # the same source is promoted once


def test_outside_prod_a_changed_source_retires_the_stable_version_it_replaces(s3, db, monkeypatch):
    name = JobPackageName.RAY_JOBS
    first = run(job_package.resolve_stable(name))
    data = b"another version of the source"
    changed = (data, package_zip.package_key(package_zip.PACKAGE_DIRS[name], hashlib.sha256(data).hexdigest()))
    monkeypatch.setattr(job_package, "build", lambda package_dir: changed)
    second = run(job_package.resolve_stable(name))
    assert second.sha256 != first.sha256
    assert {sha: row["status"] for (_, sha), row in db.rows.items()} == {first.sha256: PackageStatus.RETIRED, second.sha256: PackageStatus.STABLE}
    assert db.audit[-1] == (name, second.sha256, PackageAuditAction.PROMOTE, job_package.SELF_PUBLISHER)


def test_another_package_is_zipped_the_same_way_under_its_own_name(tmp_path):
    package = tmp_path / "ray-training" / "ray_training"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    data, key = package_zip.build(package)
    assert key == f"{layout.JOB_PACKAGE_ROOT}/ray-training-{hashlib.sha256(data).hexdigest()}.zip"
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert "package/ray_training/__init__.py" in names and "package/ray_common/__init__.py" in names


def _zip(files: dict[str, str | bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for n, content in files.items():
            zf.writestr(n, content)
    return buf.getvalue()


def _stage(s3, data: bytes, name=JobPackageName.RAY_TRAINING, sha: str | None = None) -> str:
    """Put `data` in the bucket under the key `name` and `sha` (the hash of `data` unless given) give; returns the key."""
    key = package_zip.package_key(package_zip.PACKAGE_DIRS[name], sha or hashlib.sha256(data).hexdigest())
    s3.objects[key] = data
    return key


def _register(key, name=JobPackageName.RAY_TRAINING):
    return run(job_package.register_staged(name, key, "ci"))


CORE_INIT = {"package/ray_core/__init__.py": "", "package/ray_common/__init__.py": ""}


def test_a_staged_package_is_read_back_and_registered_as_a_candidate(s3, db):
    data = _zip(CORE_INIT | {"package/ray_core/spec.py": ""})
    package = _register(_stage(s3, data))
    assert (package.sha256, package.status, package.published_by) == (hashlib.sha256(data).hexdigest(), PackageStatus.CANDIDATE, "ci")
    assert package.s3_key == package_zip.package_key(package_zip.PACKAGE_DIRS[JobPackageName.RAY_TRAINING], package.sha256)


def test_a_requirements_lock_at_the_package_root_is_stored_with_the_package(s3, db):
    lock = "# torch stack\nTorch==2.11.0+cu128\n\nnumpy==2.1.0  # pinned\n    # via torch\n"
    package = _register(_stage(s3, _zip(CORE_INIT | {"package/requirements.lock": lock})))
    assert package.requirements == ["torch==2.11.0+cu128", "numpy==2.1.0"]


def test_a_package_without_a_lock_has_no_requirements(s3, db):
    assert _register(_stage(s3, _zip(CORE_INIT))).requirements == []


@pytest.mark.parametrize("lock", ["numpy>=1.0\n", "numpy==1.0\nnumpy==1.0\n", "--index-url https://x\n", "numpy==1.0 --hash=sha256:ab\n", b"\xff\xfe"])
def test_a_lock_that_is_not_exact_pins_is_refused(s3, db, lock):
    key = _stage(s3, _zip(CORE_INIT | {"package/requirements.lock": lock}))
    with pytest.raises(Problem) as err:
        _register(key)
    assert (err.value.status_code, err.value.code) == (422, package_zip.INVALID_REQUIREMENTS) and not db.rows


@pytest.mark.parametrize("names", [
    ["package/ray_jobs/__init__.py"],  # another package's directory
    ["package/ray_core/../evil.py"], ["package/ray_core/data.bin"], ["ray_core/__init__.py"], ["setup.py"], ["requirements.lock"],
    ["package/ray_core/requirements.lock"], [],
    ["package/ray_core/__init__.py"],  # a package without the shared kernel
    ["package/ray_core/__init__.py", "package/ray_common/__init__.py", "package/ray_common/../evil.py"],
    ["package/ray_core/__init__.py", "package/ray_common/__init__.py", "package/ray_common/data.bin"],
])
def test_a_staged_package_must_be_python_files_under_its_own_directory(s3, db, names):
    key = _stage(s3, _zip({n: "" for n in names}))
    with pytest.raises(Problem) as err:
        _register(key)
    assert (err.value.status_code, err.value.code) == (422, package_zip.INVALID_PACKAGE) and not db.rows


def test_the_object_must_hash_to_the_sha_in_its_key(s3, db):
    key = _stage(s3, _zip(CORE_INIT), sha="0" * 64)
    with pytest.raises(Problem, match="hash") as err:
        _register(key)
    assert err.value.status_code == 422 and not db.rows


def test_an_object_that_is_not_a_zip_is_422(s3, db):
    with pytest.raises(Problem) as err:
        _register(_stage(s3, b"definitely not a zip"))
    assert (err.value.status_code, err.value.detail) == (422, "the object is not a zip archive")


def test_an_object_over_the_size_cap_is_422_and_never_downloaded(s3, db, monkeypatch):
    monkeypatch.setattr(package_zip, "MAX_PACKAGE_BYTES", 4)
    key = _stage(s3, b"12345")
    s3.get = AssertionError("downloaded")
    with pytest.raises(Problem) as err:
        _register(key)
    assert (err.value.status_code, err.value.code) == (422, package_zip.INVALID_PACKAGE) and s3.gets == []


@pytest.mark.parametrize("key", [
    "jobs/ray-jobs-" + "a" * 64 + ".zip",  # another package's key
    "imports/t/d/i/upload/source", "jobs/ray-training-short.zip", "../jobs/ray-training-" + "a" * 64 + ".zip", ""])
def test_only_the_key_the_name_and_hash_give_is_accepted(s3, db, key):
    with pytest.raises(Problem) as err:
        _register(key)
    assert err.value.status_code == 422 and not db.rows


def test_a_key_that_is_not_in_staging_is_refused(s3, db):
    key = package_zip.package_key(package_zip.PACKAGE_DIRS[JobPackageName.RAY_TRAINING], "a" * 64)
    with pytest.raises(Problem, match="not in the staging bucket") as err:
        _register(key)
    assert err.value.status_code == 422


def test_a_staging_error_other_than_not_found_is_not_masked_as_a_422(s3, db):
    key = _stage(s3, _zip(CORE_INIT))
    s3.head = client_error("AccessDenied")
    with pytest.raises(ClientError):
        _register(key)


def _build_script():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("build_job_package", Path(__file__).parents[4] / "scripts" / "build-job-package.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    return script


def test_the_ci_build_script_makes_the_same_zip_as_core_api():
    script = _build_script()
    for name, package_dir in package_zip.PACKAGE_DIRS.items():
        assert script.build(script.DIRS[name]) == package_zip.build(package_dir)[0], name


def test_the_real_ray_training_package_carries_its_lock_and_register_accepts_it(s3, db):
    """The zip CI builds from the source tree: package/requirements.lock holds the core's pins, which register_staged reads."""
    script = _build_script()
    data = script.build(script.DIRS["ray_training"])
    lock = zipfile.ZipFile(io.BytesIO(data)).read(f"{package_zip.ZIP_ROOT}/{package_zip.LOCK_FILE}").decode()
    package = _register(_stage(s3, data))
    assert package.requirements == parse_lock(lock) and package.requirements
    assert any(r.startswith("torch==") for r in package.requirements)
    assert package_zip.build(package_zip.TRAINING_PACKAGE_DIR)[0] == data  # self-publish outside prod ships the same lock


# ---- the smoke ----

def _candidate(db, smoke_job_id=FIRST_SMOKE_JOB, smoke=SmokeStatus.PENDING, status=PackageStatus.CANDIDATE, name=JobPackageName.RAY_JOBS, sha=SHA) -> JobPackage:
    """A package row in the table, as the lifecycle will read it back."""
    db.rows[(name, sha)] = package_row(name, sha, "jobs/x.zip", "ci", status) | {"smoke_job_id": smoke_job_id, "smoke_status": smoke}
    return JobPackage.model_validate(db.rows[(name, sha)])


def _result(s3, package, ok, attempt=""):
    s3.objects[layout.smoke_prefix(package.name, package.sha256, attempt) + layout.SMOKE_KEY] = json.dumps({"ok": ok}).encode()


class Ray:
    """The Ray job API: what `job_submit.submit` is sent and what `compute.status` answers."""

    def __init__(self, monkeypatch):
        self.sent, self.refuse = {}, None

        async def submit(**kw):
            if self.refuse:
                raise self.refuse
            self.sent.update(kw)

        async def status(job_id):
            if isinstance(self.status_answer, Exception):
                raise self.status_answer
            return self.status_answer

        self.status_answer = job_package.compute.JobStatus.RUNNING
        monkeypatch.setattr(job_package.job_submit, "submit", submit)
        monkeypatch.setattr(job_package.compute, "status", status)


@pytest.fixture
def ray(monkeypatch):
    return Ray(monkeypatch)


@pytest.mark.parametrize("ok, expected", [(True, SmokeStatus.PASSED), (False, SmokeStatus.FAILED)])
def test_a_written_smoke_result_settles_the_package(s3, db, ok, expected):
    package = _candidate(db)
    _result(s3, package, ok)
    assert run(job_package.refresh_smoke(package)).smoke_status == expected and db.rows[(package.name, SHA)]["smoke_status"] == expected


@pytest.mark.parametrize("ray_status, expected", [
    (job_package.compute.JobStatus.FAILED, SmokeStatus.FAILED), (job_package.compute.JobStatus.STOPPED, SmokeStatus.FAILED),
    (job_package.compute.JobStatus.RUNNING, SmokeStatus.PENDING)])
def test_without_a_result_a_dead_job_fails_and_a_live_one_stays_pending(s3, db, ray, ray_status, expected):
    ray.status_answer = ray_status
    package = _candidate(db)
    assert run(job_package.refresh_smoke(package)).smoke_status == expected and db.rows[(package.name, SHA)]["smoke_status"] == expected


def test_a_job_ray_cannot_answer_for_stays_pending(s3, db, ray):
    ray.status_answer = httpx.ConnectError("ray down")
    package = _candidate(db)
    assert run(job_package.refresh_smoke(package)) is package and db.rows[(package.name, SHA)]["smoke_status"] == SmokeStatus.PENDING


@pytest.mark.parametrize("smoke_job_id, smoke", [("j", SmokeStatus.PASSED), (None, SmokeStatus.PENDING)])
def test_a_settled_or_never_started_smoke_is_returned_untouched(s3, db, smoke_job_id, smoke):
    package = _candidate(db, smoke_job_id=smoke_job_id, smoke=smoke)
    s3.get = AssertionError("read the result")
    assert run(job_package.refresh_smoke(package)) is package


def test_a_smoke_result_the_bucket_cannot_serve_is_not_taken_for_a_missing_one(s3, db):
    package = _candidate(db)
    s3.get = client_error("AccessDenied")
    with pytest.raises(ClientError):
        run(job_package.refresh_smoke(package))


def test_the_smoke_job_is_submitted_under_a_stable_id_and_recorded(s3, db, ray):
    package = _candidate(db, smoke_job_id=None)
    result = run(job_package.start_smoke(package))
    job_id = FIRST_SMOKE_JOB
    sent = ray.sent
    assert result.smoke_job_id == job_id == db.rows[(package.name, SHA)]["smoke_job_id"]
    assert (sent["job_id"], sent["module"], sent["package_url"]) == (job_id, job_catalog.SMOKE_ENTRYPOINTS[JobPackageName.RAY_JOBS], "http://ray-side/jobs/x.zip")
    assert json.loads(sent["spec_json"])["prefix"] == layout.smoke_prefix(JobPackageName.RAY_JOBS, SHA)
    assert sent["timeout_seconds"] == job_package.SMOKE_TIMEOUT_SECONDS


def test_a_package_that_already_has_a_smoke_job_is_not_submitted_again(s3, db, ray):
    started = _candidate(db, smoke_job_id="smoke-1")
    ray.refuse = AssertionError("submitted twice")
    assert run(job_package.start_smoke(started)) is started


def test_a_retried_smoke_gets_a_new_job_id_and_result_prefix(s3, db, ray):
    package = _candidate(db, smoke=SmokeStatus.FAILED)
    result = run(job_package.retry_smoke(package.name, SHA))
    sent = ray.sent
    attempt = sent["job_id"].removeprefix(f"smoke-ray-jobs-{SHA[:12]}-")
    assert re.fullmatch(r"\d{14}", attempt) and result.smoke_job_id == sent["job_id"] and result.smoke_status == SmokeStatus.PENDING
    assert json.loads(sent["spec_json"])["prefix"] == layout.smoke_prefix(JobPackageName.RAY_JOBS, SHA, attempt) != layout.smoke_prefix(JobPackageName.RAY_JOBS, SHA)
    _result(s3, package, ok=False)  # the failed first run's result is not read again ...
    assert run(job_package.refresh_smoke(result)).smoke_status == SmokeStatus.PENDING
    _result(s3, package, ok=True, attempt=attempt)  # ... the retry's own is
    assert run(job_package.refresh_smoke(result)).smoke_status == SmokeStatus.PASSED


def test_a_retry_that_lost_the_compare_and_set_submits_nothing(s3, db, ray):
    package = _candidate(db, smoke=SmokeStatus.PENDING)  # another retry already reset it
    ray.refuse = AssertionError("submitted")
    assert run(job_package.retry_smoke(package.name, SHA)) is None


def test_a_retry_ray_refuses_leaves_the_package_failed(s3, db, ray):
    package = _candidate(db, smoke=SmokeStatus.FAILED)
    ray.refuse = httpx.ConnectError("ray down")
    with pytest.raises(httpx.ConnectError):
        run(job_package.retry_smoke(package.name, SHA))
    assert db.rows[(package.name, SHA)]["smoke_status"] == SmokeStatus.FAILED


# ---- the gates the admin's moves share ----

def test_a_promotion_needs_a_smoke_that_passed(s3, db):
    pending = _candidate(db)
    with pytest.raises(Problem) as err:
        run(job_package.require_smoke_passed(pending, "not yet"))
    assert (err.value.status_code, err.value.code, err.value.detail) == (409, job_package.SmokeProblem.NOT_PASSED, "not yet")
    _result(s3, pending, ok=True)
    run(job_package.require_smoke_passed(pending, "not yet"))


@pytest.mark.parametrize("status, smoke", [
    (PackageStatus.STABLE, SmokeStatus.FAILED), (PackageStatus.RETIRED, SmokeStatus.FAILED),
    (PackageStatus.CANDIDATE, SmokeStatus.PENDING), (PackageStatus.CANDIDATE, SmokeStatus.PASSED)])
def test_only_a_failed_candidate_is_smoked_again(s3, db, ray, status, smoke):
    package = _candidate(db, status=status, smoke=smoke)
    ray.refuse = AssertionError("submitted")
    with pytest.raises(Problem) as err:
        run(job_package.retry_failed_smoke(package, "only failed ones"))
    assert (err.value.status_code, err.value.code, err.value.detail) == (409, job_package.SmokeProblem.NOT_RETRYABLE, "only failed ones")


def test_a_failed_candidate_is_smoked_again_and_a_retry_another_request_won_is_a_409(s3, db, ray):
    package = _candidate(db, smoke=SmokeStatus.FAILED)
    assert run(job_package.retry_failed_smoke(package, "x")).smoke_status == SmokeStatus.PENDING
    with pytest.raises(Problem) as err:
        run(job_package.retry_failed_smoke(package.model_copy(), "x"))  # the same stale read: the table no longer has it failed
    assert err.value.code == job_package.SmokeProblem.NOT_RETRYABLE
