"""The ray_jobs zip: deterministic, content-addressed, and importable by the entrypoint bootstrap."""
import asyncio
import hashlib
import io
import shlex
import subprocess
import sys
import zipfile

from app.integrations.data_plane import ingest, job_catalog, job_package, layout


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


def test_presigned_url_uploads_a_new_version_once(monkeypatch):
    calls = []

    class S3:
        def head_object(self, **kw):
            from botocore.exceptions import ClientError

            calls.append("head")
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")

        def put_object(self, **kw):
            calls.append(("put", kw["Key"]))

        def generate_presigned_url(self, op, Params, ExpiresIn):
            return f"http://ray-side/{Params['Key']}"

    monkeypatch.setattr(job_package.lakefs, "_s3", lambda secret, endpoint=None: S3())
    monkeypatch.setattr(job_package, "_uploaded", set())
    _, key = job_package.build()
    assert asyncio.run(job_package.presigned_url()) == f"http://ray-side/{key}"
    asyncio.run(job_package.presigned_url())
    assert calls == ["head", ("put", key)]  # the second start finds it in this process's memory
