"""The generated Ray scripts read/write models in the platform bucket from the `s3` secret."""
import asyncio
import base64
import re

from app.core import secret_files
from app.integrations import ray_client, serve_client

S3 = {"AWS_ACCESS_KEY_ID": "AK", "AWS_SECRET_ACCESS_KEY": "SK", "S3_ENDPOINT": "https://store.test", "S3_BUCKET": "plat"}
LAKEFS = {"endpoint": "http://lakefs:8000", "access_key": "LK", "secret_key": "LS"}


def _script_of(entrypoint: str) -> str:
    return base64.b64decode(re.search(r"b64decode\('([^']+)'", entrypoint).group(1)).decode()


def _capture(monkeypatch):
    monkeypatch.setattr(secret_files, "get", lambda name: {"s3": S3, "lakefs": LAKEFS}[name])
    seen = []

    async def submit(*, entrypoint, **_):
        seen.append(_script_of(entrypoint))
        return "job-1"

    monkeypatch.setattr(ray_client, "submit_raw_job", submit)
    return seen


def test_onnx_serve_script_reads_the_platform_bucket(monkeypatch):
    seen = _capture(monkeypatch)
    weights = {"onnx_key": "models/7/model.onnx", "class_names": ["a", "b"]}
    asyncio.run(serve_client.deploy(deployment_id="d", weights=weights, tenant_id="t", project_id="p"))
    script = seen[0]
    compile(script, "serve", "exec")
    assert "'https://store.test'" in script and "'plat'" in script and "minio" not in script


def test_ultralytics_train_script_writes_models_to_the_platform_bucket(monkeypatch):
    seen = _capture(monkeypatch)
    asyncio.run(
        ray_client.submit_job(
            tenant_id="t", project_id="p", dataset_ref="r", config={"backend": "ultralytics"}, file_keys=["a/b.jpg"]
        )
    )
    script = seen[0]
    compile(script, "train", "exec")
    assert "Bucket='plat'" in script and "models/" in script and "minio" not in script
