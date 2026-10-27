"""Training runs on Ray: stage what the job reads, submit the driver, read what it wrote.

The driver is the `ray_training` package (top-level ray-training/). It runs on the CPU pool (no GPU of its own: the
GPU belongs to the Ray task it starts, docs/trainer-plan.md "Ray execution") and gets the `ingest` credential
(staging bucket only), a spec of presigned reads, and no lakeFS or MLflow credential. Everything it writes goes under
the run's prefix (layout.train_prefix); core-api reads report.json and publishes the model (mlflow.publish_run_model).
"""
import asyncio
import json

from botocore.exceptions import ClientError

from app.core import secret_files
from app.core.settings import settings
from app.integrations import compute
from app.integrations.data_plane import access, ingest, job_catalog, job_package, lakefs, layout
from app.integrations.data_plane.train_report import TrainReport
from app.integrations.job_spec import TrainData, TrainSpec

_S3_NO_SUCH_KEY = "NoSuchKey"
INDEX_PATH_COLUMN = "path"


async def prepare_train_data(tenant_id: str, project_id: str, dataset_id: str, version_id: str, run_id: str, ttl_seconds: int) -> TrainData:
    """Presign the version's index and every image it lists, stage {index path: url} as the run's manifest.json, presign the
    manifest. All reads stay valid for `ttl_seconds`, which must outlast the run's queue time and `max_runtime_s`."""
    commit = await access.version_commit(tenant_id, dataset_id, version_id)
    paths = await lakefs.read_version_column(tenant_id, dataset_id, version_id, layout.INDEX_KEY, INDEX_PATH_COLUMN)
    keys = [layout.OBJECTS_PREFIX + p for p in paths] + [layout.INDEX_KEY]
    urls = await lakefs.presign_version_objects(tenant_id, dataset_id, version_id, keys, ttl_seconds)
    index_url = urls.pop(layout.INDEX_KEY)
    manifest_body = {key.removeprefix(layout.OBJECTS_PREFIX): url for key, url in urls.items()}
    key = layout.train_prefix(tenant_id, project_id, run_id) + layout.MANIFEST_KEY
    bucket = settings.data_plane.staging_bucket
    await asyncio.to_thread(lakefs.s3_client(secret_files.STAGING).put_object, Bucket=bucket, Key=key, Body=json.dumps(manifest_body).encode())
    manifest = lakefs.s3_client(secret_files.INGEST).generate_presigned_url(
        "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=ttl_seconds)
    return TrainData(index_url=index_url, manifest_url=manifest, version_id=version_id, lakefs_commit=commit)


async def submit_train(tenant_id: str, project_id: str, run_id: str, spec: TrainSpec) -> str:
    """Submit the driver under the spec's job_id (the caller's submission id); returns the Ray job id."""
    await access.training_run(tenant_id, project_id, run_id)
    creds = secret_files.get(secret_files.INGEST)
    package_url = await job_package.presigned_url(job_package.TRAINING_PACKAGE_DIR)
    return await compute.submit(compute.JobSpec(
        entrypoint=ingest.command(package_url, job_catalog.TRAIN_ENTRYPOINT, spec.model_dump_json()),
        submission_id=spec.job_id,
        metadata={"tenant_id": tenant_id, "project_id": project_id, "training_run_id": run_id},
        num_cpus=settings.training.driver_cpus,
        num_gpus=0,  # the driver must not hold the only GPU: its Ray task asks for it (spec.resources.gpu)
        resources={compute.CPU_POOL_RESOURCE: 1},
        timeout_seconds=spec.resources.max_runtime_s,
        env_vars={
            "STAGING_ENDPOINT": creds["endpoint"],
            "STAGING_ACCESS_KEY": creds["access_key"],
            "STAGING_SECRET_KEY": creds["secret_key"],
        },
    ))


async def read_train_report(tenant_id: str, project_id: str, run_id: str) -> TrainReport | None:
    """The job's report.json, or None while it has not been written. A report that does not validate raises pydantic's ValidationError."""
    await access.training_run(tenant_id, project_id, run_id)
    key = layout.train_prefix(tenant_id, project_id, run_id) + layout.REPORT_KEY
    try:
        obj = await asyncio.to_thread(lakefs.s3_client(secret_files.STAGING).get_object, Bucket=settings.data_plane.staging_bucket, Key=key)
    except ClientError as e:
        if e.response["Error"]["Code"] == _S3_NO_SUCH_KEY:
            return None
        raise
    return TrainReport.model_validate_json(await asyncio.to_thread(obj["Body"].read))
