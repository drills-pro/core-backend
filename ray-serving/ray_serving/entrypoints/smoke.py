"""Smoke job: the package imports on this cluster and answers a request with a tiny model, before it can be promoted.

    python -m ray_serving.entrypoints.smoke '{"job_id": ..., "bucket": ..., "prefix": ...}'      # STAGING_* in the environment

Imports the Serve app (so ray.serve must be there), builds a 4x4 two-class ONNX model, serves it through model.load and
model.predict from file:// URLs, and writes smoke.json. It does not start Serve itself.
"""
import base64
import io
import json
import os
import sys
import tempfile
from pathlib import Path

from ray_serving import model
from ray_serving.constants import SMOKE_KEY

SIZE = 4
CLASSES = ["a", "b"]
ENCODER, POSTPROCESS = "image_encoder", "classify"


def tiny_model(path: Path) -> None:
    """pixel_values (batch, 3, 4, 4) -> logits (batch, 2): flatten, then one fixed matrix."""
    import numpy as np
    import onnx
    from onnx import TensorProto, helper, numpy_helper

    weights = numpy_helper.from_array(np.linspace(-1, 1, 3 * SIZE * SIZE * len(CLASSES), dtype=np.float32).reshape(-1, len(CLASSES)), "w")
    graph = helper.make_graph(
        [helper.make_node("Flatten", ["pixel_values"], ["flat"], axis=1), helper.make_node("MatMul", ["flat", "w"], ["logits"])],
        "tiny", [helper.make_tensor_value_info("pixel_values", TensorProto.FLOAT, ["batch", 3, SIZE, SIZE])],
        [helper.make_tensor_value_info("logits", TensorProto.FLOAT, ["batch", len(CLASSES)])], [weights])
    onnx.save(helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)], ir_version=8), str(path))


def check() -> None:
    from PIL import Image

    from ray_serving import app

    app.build({"max_models_per_replica": 1})
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        tiny_model(work / "model.onnx")
        (work / "model.json").write_text(json.dumps({"preprocessing": {
            "resize": [SIZE, SIZE], "resample": "bilinear", "rescale": 1 / 255, "mean": [0.5] * 3, "std": [0.25] * 3}}))
        spec = model.ModelSpec(
            model_url=(work / "model.onnx").as_uri(), manifest_url=(work / "model.json").as_uri(), tokenizer_url="", class_names=CLASSES,
            params={}, providers=["CPUExecutionProvider"], encoder=ENCODER, postprocess=POSTPROCESS)
        png = io.BytesIO()
        Image.new("RGB", (8, 8), (200, 20, 20)).save(png, "PNG")
        answer = model.predict(model.load(spec), {"image_base64": base64.b64encode(png.getvalue()).decode()})
    assert sorted(p["class"] for p in answer) == CLASSES, answer


def main(argv: list[str], store=None) -> int:
    """Exit code 1 when the check failed. `store` is the staging bucket under the job's prefix; by default S3 from the environment."""
    arg = json.loads(argv[1])
    if store is None:
        import boto3

        client = boto3.client("s3", endpoint_url=os.environ["STAGING_ENDPOINT"], aws_access_key_id=os.environ["STAGING_ACCESS_KEY"],
                              aws_secret_access_key=os.environ["STAGING_SECRET_KEY"])
        store = type("S3Store", (), {"put": lambda self, key, data: client.put_object(Bucket=arg["bucket"], Key=arg["prefix"] + key, Body=data)})()
    try:
        check()
        ok = True
    except Exception as e:  # noqa: BLE001 - the verdict is written either way; the log has the trace
        print(f"smoke failed: {type(e).__name__}: {e}", file=sys.stderr)
        ok = False
    store.put(SMOKE_KEY, json.dumps({"ok": ok}).encode())
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
