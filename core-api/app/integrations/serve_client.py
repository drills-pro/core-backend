"""Ray Serve deployment (req/04 Deployments group, req/07 FR3: Ray Serve).

Deploying is itself a Ray job: the entrypoint calls `serve.run(...)`, which
starts the Serve replica as an independent Ray actor and returns — the
submission job then exits, but the deployment keeps running (this is the
standard way to drive Serve from outside the cluster's own Python process).
core-api then calls the deployed HTTP route directly for /predict.
"""
import ast
import base64
import textwrap
from importlib import resources
from enum import StrEnum
from urllib.parse import urlsplit

from app.core.settings import settings
from app.core.task_catalog import IMAGENET_PREPROCESSING
from app.core.task_types import TaskInfo
from app.integrations import compute
from app.integrations.data_plane import layout, mlflow

SERVE_HTTP_PORT = 8000  # Serve's proxy port on the Ray head; the serve scripts below start it on the same one

# Both files are sent to the replica as text and never imported here: core-api depends on neither numpy nor PIL.
INPUTS_FILE = "serve_inputs.py"
POSTPROCESS_FILE = "serve_postprocess.py"


def _source(name: str) -> str:
    return resources.files(__package__).joinpath(name).read_text()


def _functions(name: str) -> set[str]:
    return {n.name for n in ast.parse(_source(name)).body if isinstance(n, ast.FunctionDef)}


def unknown_serving_functions(encoder: str | None, postprocess: str | None) -> list[str]:
    """The names, of those given, that the serve files do not define."""
    missing = [] if encoder is None or encoder in _functions(INPUTS_FILE) else [encoder]
    return missing + ([] if postprocess is None or postprocess in _functions(POSTPROCESS_FILE) else [postprocess])


class ComputeTarget(StrEnum):
    """Where a deployment runs; also the project setting `compute_target`."""

    CPU = "cpu"
    GPU = "gpu"


def serve_url() -> str:
    """Where deployed routes are served: the configured Ray head (settings.ray.dashboard) at Serve's port, read per call."""
    head = urlsplit(settings.ray.dashboard)
    return f"{head.scheme}://{head.hostname}:{SERVE_HTTP_PORT}"


# req/06 Ultralytics FR3: serves the actual exported ONNX model — real
# onnxruntime inference on real weights, not a stub. Accepts a base64
# image and preprocesses it as model.json says the training job did.
_SERVE_TEMPLATE = """
import json
import urllib.request
from ray import serve
from starlette.requests import Request
import onnxruntime as ort

class_names = {class_names!r}
params = {params!r}
model_url = {model_url!r}
manifest_url = {manifest_url!r}
tokenizer_url = {tokenizer_url!r}

@serve.deployment
class Predictor:
    def __init__(self):
        # Loaded lazily inside the deployment, not at module scope — an
        # onnxruntime InferenceSession can't be pickled, and Ray Serve
        # only pickles the class definition to distribute it, not
        # module-level objects captured in a closure.
        # The model is an artifact of the training run's MLflow run (core-api authorized this deployment).
        onnx_bytes = urllib.request.urlopen(model_url, timeout=120).read()
        self.session = ort.InferenceSession(onnx_bytes, providers={providers!r})
        self.input_name = self.session.get_inputs()[0].name
        # a model trained before the manifest named its resampling filter used the library default
        self.pre = {{"resample": {default_resample!r}, **json.loads(urllib.request.urlopen(manifest_url, timeout=120).read())["preprocessing"]}}
        self.encode = {encoder}(self.pre, tokenizer_url, self.input_name)

    async def __call__(self, request: Request):
        body = await request.json()
        feeds, width, height = self.encode(body)
        outputs = self.session.run(None, feeds)
        return {{"predictions": {postprocess}(outputs, class_names, width, height, params)}}

serve.start(http_options={{"host": "0.0.0.0", "port": {port}}})
serve.run(Predictor.bind(), name="{name}", route_prefix="{route_prefix}")
print("metric:deployed=1")
"""


async def deploy(
    *,
    deployment_id: str,
    task: TaskInfo,
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
    params = task.serving.model_dump(mode="json")
    script = _source(INPUTS_FILE) + _source(POSTPROCESS_FILE) + textwrap.dedent(_SERVE_TEMPLATE).format(
        encoder=task.encoder,
        postprocess=task.postprocess,
        providers=params.pop("providers"),
        params=params,
        tokenizer_url=model_url.removesuffix(layout.MODEL_KEY) + layout.TOKENIZER_KEY,
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
