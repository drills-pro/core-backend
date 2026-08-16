"""Ray Serve deployment (req/04 Deployments group, req/07 FR3: Ray Serve).

Deploying is itself a Ray job: the entrypoint calls `serve.run(...)`, which
starts the Serve replica as an independent Ray actor and returns — the
submission job then exits, but the deployment keeps running (this is the
standard way to drive Serve from outside the cluster's own Python process).
core-api then calls the deployed HTTP route directly for /predict.
"""
import base64
import textwrap

from app import ray_client, vault

RAY_SERVE_HTTP = "http://ray-head:8000"

# Serves the exact logistic-regression weights a numpy-backend training run
# actually fit (see app/ray_client.py's model: log line) — real inference
# on a real (if simple) fitted model, not a stub response.
_SERVE_TEMPLATE = """
import numpy as np
from ray import serve
from starlette.requests import Request

w = np.array({w})
b = {b}

@serve.deployment
class Predictor:
    async def __call__(self, request: Request):
        body = await request.json()
        x = np.array(body["features"])
        z = float(x @ w + b)
        p = 1 / (1 + np.exp(-z))
        return {{"probability": p, "prediction": int(p > 0.5)}}

serve.start(http_options={{"host": "0.0.0.0", "port": 8000}})
serve.run(Predictor.bind(), name="{name}", route_prefix="{route_prefix}")
print("metric:deployed=1")
"""


# req/06 Ultralytics FR3: serves the actual exported ONNX model — real
# onnxruntime inference on real weights, not a stub. Accepts a base64
# image, applies the same 32x32 preprocessing the training job used.
_ONNX_SERVE_TEMPLATE = """
import base64, io, json
import numpy as np
import boto3
from botocore.client import Config
from PIL import Image
from ray import serve
from starlette.requests import Request
import onnxruntime as ort

class_names = {class_names!r}
onnx_key = {onnx_key!r}
s3_endpoint = {s3_endpoint!r}
s3_access_key = {s3_access_key!r}
s3_secret_key = {s3_secret_key!r}

@serve.deployment
class Predictor:
    def __init__(self):
        # Loaded lazily inside the deployment, not at module scope — an
        # onnxruntime InferenceSession can't be pickled, and Ray Serve
        # only pickles the class definition to distribute it, not
        # module-level objects captured in a closure.
        s3 = boto3.client("s3", endpoint_url=s3_endpoint,
            aws_access_key_id=s3_access_key, aws_secret_access_key=s3_secret_key,
            config=Config(signature_version="s3v4"))
        onnx_bytes = s3.get_object(Bucket="datasets", Key=onnx_key)["Body"].read()
        self.session = ort.InferenceSession(onnx_bytes, providers=["CPUExecutionProvider"])
        self.input_name = self.session.get_inputs()[0].name

    async def __call__(self, request: Request):
        body = await request.json()
        img = Image.open(io.BytesIO(base64.b64decode(body["image_base64"]))).convert("RGB").resize((32, 32))
        arr = (np.asarray(img).astype(np.float32) / 255.0).transpose(2, 0, 1)[None, ...]
        logits = self.session.run(None, {{self.input_name: arr}})[0][0]
        probs = np.exp(logits) / np.sum(np.exp(logits))
        ranked = sorted(zip(class_names, probs.tolist()), key=lambda x: -x[1])
        return {{"predictions": [{{"class": c, "probability": p}} for c, p in ranked]}}

serve.start(http_options={{"host": "0.0.0.0", "port": 8000}})
serve.run(Predictor.bind(), name="{name}", route_prefix="{route_prefix}")
print("metric:deployed=1")
"""


async def deploy(
    *,
    deployment_id: str,
    weights: dict,
    tenant_id: str,
    project_id: str,
    compute_target: str = "cpu",
    deploy_strategy: str = "latest",
) -> tuple[str, str]:
    # FR4/FR5 (06-project-settings-deployment.md): 'main' reuses one stable
    # route per project across deploys (serve.run against the same `name`
    # updates the running replica in place); 'latest' keeps Phase 1's
    # behavior of a permanent per-deployment route.
    if deploy_strategy == "main":
        route_prefix = f"/predict/{project_id}/main"
        name = f"deploy-project-{project_id}-main"
    else:
        route_prefix = f"/predict/{deployment_id}"
        name = f"deploy-{deployment_id}"

    if "onnx_key" in weights:
        minio = vault.get_kv("platform/minio")
        script = textwrap.dedent(_ONNX_SERVE_TEMPLATE).format(
            onnx_key=weights["onnx_key"],
            class_names=weights["class_names"],
            s3_endpoint="http://minio:9000",
            s3_access_key=minio["access_key"],
            s3_secret_key=minio["secret_key"],
            name=name,
            route_prefix=route_prefix,
        )
    else:
        script = textwrap.dedent(_SERVE_TEMPLATE).format(
            w=weights["w"], b=weights["b"], name=name, route_prefix=route_prefix
        )

    encoded = base64.b64encode(script.encode()).decode()
    entrypoint = f'python -c "import base64; exec(base64.b64decode(\'{encoded}\'))"'
    job_id = await ray_client.submit_raw_job(
        entrypoint=entrypoint,
        metadata={"tenant_id": tenant_id, "project_id": project_id, "job_type": "serve-deploy"},
        num_gpus=1 if compute_target == "gpu" else 0,
    )
    return job_id, route_prefix
