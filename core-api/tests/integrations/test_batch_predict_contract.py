"""The contract between core-api's BatchPredictSpec and the batch runner in the ray_serving package (top-level ray-serving/): the
runner mirrors core-api's constants instead of importing them, so these tests fail when the two drift."""
import ast
import uuid
from pathlib import Path

from app.integrations import job_spec
from app.integrations.data_plane import layout
from app.integrations.job_spec import BatchData, BatchModel, BatchPredictPayload, BatchPredictSpec, JobKind, Resources
from app.integrations.serve_client import ServedModel
from ray_jobs.common import enums as ingest_enums
from ray_serving import batch_spec
from ray_serving import constants as batch
from ray_core import constants as trainer


RAY_SERVING_MODEL = Path(__file__).parents[3] / "ray-serving" / "ray_serving" / "model.py"  # read as text: core-api has no numpy or onnxruntime


def spec() -> BatchPredictSpec:
    served = ServedModel(
        model_url="http://mlflow/a/model.onnx", manifest_url="http://mlflow/a/model.json", tokenizer_url="", class_names=["a", "b"],
        params={"top_k": 2}, providers=["CPUExecutionProvider"], encoder="image_encoder", postprocess="classify")
    payload = BatchPredictPayload(
        bucket="b", prefix="batch/p/", data=BatchData(index_url="http://i", manifest_url="http://m", version_id=uuid.uuid4(), lakefs_commit="c0ffee"),
        model=BatchModel(**served.model_dump()), concurrency=3, batch_size=8)
    return BatchPredictSpec(job_id="j", tenant_id=uuid.uuid4(), resources=Resources(cpu=2, memory_bytes=1), payload=payload)


def test_the_runner_mirrors_the_spec_constants_and_the_layout():
    assert (batch.SPEC_VERSION, batch.PREDICT_BATCH_KIND) == (job_spec.SPEC_VERSION, JobKind.PREDICT_BATCH.value)
    assert (batch.PREDICTIONS_KEY, batch.REPORT_KEY) == (layout.BATCH_PREDICTIONS_KEY, layout.REPORT_KEY)
    assert batch_spec.INDEX_PATH_COLUMN == layout.INDEX_COLUMNS[0]


def test_every_job_kind_is_run_by_exactly_one_package():
    # ray_common now owns JobKind; each package advertises only the runner it can run.
    assert ingest_enums.JobKind.INGEST == JobKind.INGEST
    assert trainer.JobKind.TRAIN == JobKind.TRAIN
    assert batch.PREDICT_BATCH_KIND == JobKind.PREDICT_BATCH.value
    assert set(JobKind) == {ingest_enums.JobKind.INGEST, trainer.JobKind.TRAIN, batch.PREDICT_BATCH_KIND}


def test_the_batch_model_is_the_replicas_model_spec_and_core_apis_served_model():
    (node,) = [n for n in ast.parse(RAY_SERVING_MODEL.read_text()).body if isinstance(n, ast.ClassDef) and n.name == "ModelSpec"]
    replica = [n.target.id for n in node.body if isinstance(n, ast.AnnAssign)]
    assert replica == list(BatchModel.model_fields) == list(ServedModel.model_fields)


def test_the_runner_reads_what_core_api_writes():
    written = spec()
    payload = batch_spec.parse_spec(written.model_dump_json())
    assert (payload["bucket"], payload["prefix"], payload["concurrency"], payload["batch_size"]) == ("b", "batch/p/", 3, 8)
    assert {"index_url", "manifest_url", "version_id", "lakefs_commit"} <= set(payload["data"])
    assert set(payload["model"]) == set(BatchModel.model_fields)
    assert batch_spec.allowed_base(payload["model"]["model_url"]) == "http://mlflow/a"
