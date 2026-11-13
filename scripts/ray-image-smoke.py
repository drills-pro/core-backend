"""Smoke test of a Ray image, run INSIDE it (.github/workflows/ray-image.yml mounts the repo at /src and calls this).

Needs no network and no S3. It fails on what broke prod: a missing torch/onnx, an `import onnx` protobuf mismatch, a package
module that cannot import. Every flag is a check; the exit code is non-zero on the first failure.

    --imports MOD...        third-party modules this image must provide
    --packages PKG...       import every module of these packages (the source dirs are on PYTHONPATH)
    --modules MOD...        single modules to import (a package whose other modules this image does not carry)
    --serving-check         ray_serving's own smoke: build a tiny ONNX model, serve it with model.load + model.predict
    --training-smoke        ray_training's own smoke: start a local Ray head and run the real Ray Train job on 8 images, then export ONNX
"""
import argparse
import importlib
import json
import os
import pkgutil
import subprocess
import sys
import tempfile
from pathlib import Path

NO_GPUS = "0"  # the runner has no GPU: one CPU Train worker
TRAIN_TIMEOUT_S = 900


def import_all(package: str) -> None:
    root = importlib.import_module(package)
    names = [m.name for m in pkgutil.walk_packages(root.__path__, f"{package}.")]
    for name in names:
        importlib.import_module(name)
    print(f"ok   {package}: {len(names)} modules")


def training_smoke() -> None:
    from ray_training.constants import SMOKE_KEY

    with tempfile.TemporaryDirectory() as staging:
        env = {**os.environ, "STAGING_ENDPOINT": f"file://{staging}", "TRAIN_NUM_GPUS": NO_GPUS}  # PYTHONPATH too: Ray's workers inherit it
        spec = json.dumps({"job_id": "image-smoke", "bucket": "smoke", "prefix": "image-smoke/"})
        subprocess.run(["ray", "start", "--head", "--num-cpus=4", "--num-gpus=0", "--include-dashboard=false", "--disable-usage-stats"],
                       env=env, check=True)
        try:
            subprocess.run([sys.executable, "-m", "ray_training.entrypoints.smoke", spec], env=env, check=True, timeout=TRAIN_TIMEOUT_S)
        finally:
            subprocess.run(["ray", "stop", "--force"], check=False)
        verdict = json.loads((Path(staging) / "image-smoke" / SMOKE_KEY).read_text())
        assert verdict == {"ok": True}, verdict
    print("ok   ray_training.entrypoints.smoke (Ray Train run + ONNX export)")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--imports", nargs="*", default=[])
    p.add_argument("--packages", nargs="*", default=[])
    p.add_argument("--modules", nargs="*", default=[])
    p.add_argument("--serving-check", action="store_true")
    p.add_argument("--training-smoke", action="store_true")
    a = p.parse_args()
    for name in [*a.imports, *a.modules]:
        importlib.import_module(name)
        print(f"ok   import {name}")
    for package in a.packages:
        import_all(package)
    if a.serving_check:
        from ray_serving.entrypoints import smoke

        smoke.check()
        print("ok   ray_serving.entrypoints.smoke.check (tiny ONNX model served)")
    if a.training_smoke:
        training_smoke()


if __name__ == "__main__":
    main()
