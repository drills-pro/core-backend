"""Ray Serve deployment (req/04 Deployments group, req/07 FR3: Ray Serve).

Deploying is itself a Ray job: the entrypoint calls `serve.run(...)`, which
starts the Serve replica as an independent Ray actor and returns — the
submission job then exits, but the deployment keeps running (this is the
standard way to drive Serve from outside the cluster's own Python process).
core-api then calls the deployed HTTP route directly for /predict.
"""
import base64
import textwrap
from enum import StrEnum
from urllib.parse import urlsplit

from app.core.settings import settings
from app.core.task_catalog import IMAGENET_PREPROCESSING
from app.integrations import compute
from app.integrations.data_plane import layout, mlflow

SERVE_HTTP_PORT = 8000  # Serve's proxy port on the Ray head; the serve scripts below start it on the same one


class ComputeTarget(StrEnum):
    """Where a deployment runs; also the project setting `compute_target`."""

    CPU = "cpu"
    GPU = "gpu"


def serve_url() -> str:
    """Where deployed routes are served: the configured Ray head (settings.ray.dashboard) at Serve's port, read per call."""
    head = urlsplit(settings.ray.dashboard)
    return f"{head.scheme}://{head.hostname}:{SERVE_HTTP_PORT}"


# What the trainer did to every training image, read from the `preprocessing` entry of the model's model.json
# (ray_training.result.load_image). No braces: this part is not run through str.format.
_PREPROCESS = """
import io
import numpy as np
from PIL import Image


def preprocess(data, pre):
    width, height = pre["resize"]
    img = Image.open(io.BytesIO(data)).convert("RGB").resize((width, height), getattr(Image, pre["resample"].upper()))
    arr = (np.asarray(img, dtype=np.float32) * pre["rescale"] - np.array(pre["mean"], dtype=np.float32)) / np.array(pre["std"], dtype=np.float32)
    return arr.transpose(2, 0, 1)[None, ...]
"""

# req/06 Ultralytics FR3: serves the actual exported ONNX model — real
# onnxruntime inference on real weights, not a stub. Accepts a base64
# image and preprocesses it as model.json says the training job did.
_SERVE_TEMPLATE = """
import base64, json
import numpy as np
import urllib.request
from ray import serve
from starlette.requests import Request
import onnxruntime as ort

class_names = {class_names!r}
model_url = {model_url!r}
manifest_url = {manifest_url!r}

@serve.deployment
class Predictor:
    def __init__(self):
        # Loaded lazily inside the deployment, not at module scope — an
        # onnxruntime InferenceSession can't be pickled, and Ray Serve
        # only pickles the class definition to distribute it, not
        # module-level objects captured in a closure.
        # The model is an artifact of the training run's MLflow run (core-api authorized this deployment).
        onnx_bytes = urllib.request.urlopen(model_url, timeout=120).read()
        self.session = ort.InferenceSession(onnx_bytes, providers=["CPUExecutionProvider"])
        self.input_name = self.session.get_inputs()[0].name
        # a model trained before the manifest named its resampling filter used the library default
        self.pre = {{"resample": {default_resample!r}, **json.loads(urllib.request.urlopen(manifest_url, timeout=120).read())["preprocessing"]}}

    async def __call__(self, request: Request):
        body = await request.json()
        arr = preprocess(base64.b64decode(body["image_base64"]), self.pre)
        logits = self.session.run(None, {{self.input_name: arr}})[0][0]
        probs = np.exp(logits) / np.sum(np.exp(logits))
        ranked = sorted(zip(class_names, probs.tolist()), key=lambda x: -x[1])
        return {{"predictions": [{{"class": c, "probability": p}} for c, p in ranked]}}

serve.start(http_options={{"host": "0.0.0.0", "port": {port}}})
serve.run(Predictor.bind(), name="{name}", route_prefix="{route_prefix}")
print("metric:deployed=1")
"""


async def deploy(
    *,
    deployment_id: str,
    mlflow_run_id: str,
    class_names: list[str],
    tenant_id: str,
    project_id: str,
    compute_target: ComputeTarget = ComputeTarget.CPU,
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

    model_url = await mlflow.run_model_url(tenant_id, project_id, mlflow_run_id)
    script = textwrap.dedent(_PREPROCESS) + textwrap.dedent(_SERVE_TEMPLATE).format(
        model_url=model_url,
        manifest_url=model_url.removesuffix(layout.MODEL_KEY) + layout.MODEL_META_KEY,  # published side by side
        default_resample=IMAGENET_PREPROCESSING.resample.value,
        class_names=class_names,
        name=name,
        route_prefix=route_prefix,
        port=SERVE_HTTP_PORT,
    )

    encoded = base64.b64encode(script.encode()).decode()
    entrypoint = f'python -c "import base64; exec(base64.b64decode(\'{encoded}\'))"'
    job_id = await compute.submit(compute.JobSpec(
        entrypoint=entrypoint,
        metadata={"tenant_id": tenant_id, "project_id": project_id, "job_type": "serve-deploy"},
        num_gpus=1 if compute_target == ComputeTarget.GPU else 0,
    ))
    return job_id, route_prefix
