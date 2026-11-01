"""The driver against a real local Ray: `ray start --head` with no GPU, the driver as a subprocess like a Ray job entrypoint.

Starts and stops a local Ray head (`ray stop --force` at the end: do not run it next to a Ray cluster you care about).
"""
import http.server
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from ray_training.constants import REPORT_KEY
from tests.support import classification_rows, parquet, spec_doc

ROOT = Path(__file__).resolve().parents[1]
RAY = Path(sys.executable).parent / "ray"


@pytest.fixture(scope="module")
def head(tmp_path_factory):
    """The staging directory; the cluster is started with the staging env vars, as a submitted job's runtime_env would carry them."""
    staging = tmp_path_factory.mktemp("staging")
    with socket.socket() as s:
        s.bind(("", 0))
        port = s.getsockname()[1]
    env = {**os.environ, "PYTHONPATH": str(ROOT), "STAGING_ENDPOINT": f"file://{staging}", "TRAIN_NUM_GPUS": "0"}
    subprocess.run([RAY, "start", "--head", f"--port={port}", "--num-cpus=4", "--num-gpus=0", "--include-dashboard=false",
                    "--disable-usage-stats"], env=env, check=True, capture_output=True)
    yield staging
    subprocess.run([RAY, "stop", "--force"], capture_output=True)


@pytest.fixture
def hang_server():
    """An index URL that never answers, so the task is stuck when the time limit hits."""
    class Hang(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            time.sleep(60)

        def log_message(self, *_):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Hang)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/index.parquet"
    server.shutdown()


def drive(staging, doc) -> tuple[subprocess.CompletedProcess, dict]:
    env = {**os.environ, "PYTHONPATH": str(ROOT), "STAGING_ENDPOINT": f"file://{staging}", "TRAIN_NUM_GPUS": "0"}
    proc = subprocess.run([sys.executable, "-m", "ray_training.entrypoints.train", json.dumps(doc)], env=env,
                          capture_output=True, text=True, timeout=300)
    report = json.loads((staging / doc["payload"]["prefix"] / REPORT_KEY).read_text())
    return proc, report


def local_doc(tmp_path, tiny_convnext, drop_last=False, **resources):
    rows, files = classification_rows()
    data = tmp_path / "data"
    data.mkdir()
    (data / "index.parquet").write_bytes(parquet(rows))
    for path, blob in files.items():
        (data / path.replace("/", "_")).write_bytes(blob)
    doc = spec_doc("image-classification", tiny_convnext, rows)
    doc["payload"]["prefix"] = f"{tmp_path.name}/"
    doc["payload"]["data"]["index_url"] = (data / "index.parquet").as_uri()
    listed = {r["path"]: (data / r["path"].replace("/", "_")).as_uri() for r in rows[:-1 if drop_last else None]}
    (data / "manifest.json").write_text(json.dumps(listed))
    doc["payload"]["data"]["manifest_url"] = (data / "manifest.json").as_uri()
    doc["resources"].update(resources)
    return doc


def test_driver_runs_the_task_on_ray_and_writes_the_report(head, tmp_path, tiny_convnext):
    proc, report = drive(head, local_doc(tmp_path, tiny_convnext))
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert report["status"] == "succeeded" and set(report["metrics"]) == {"top1", "top5"}
    assert (head / f"{tmp_path.name}/model.onnx").stat().st_size > 0


def test_a_failure_inside_the_task_still_ends_in_a_report(head, tmp_path, tiny_convnext):
    proc, report = drive(head, local_doc(tmp_path, tiny_convnext, drop_last=True))
    assert proc.returncode == 1 and report["status"] == "failed" and report["code"] == "bad_data"


def test_the_time_limit_cancels_the_task_and_reports_timeout(head, tmp_path, tiny_convnext, hang_server):
    doc = local_doc(tmp_path, tiny_convnext, max_runtime_s=3)
    doc["payload"]["data"]["index_url"] = hang_server
    proc, report = drive(head, doc)
    assert proc.returncode == 1 and report["code"] == "timeout"
