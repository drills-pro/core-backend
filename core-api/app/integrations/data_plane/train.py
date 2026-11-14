"""Training runs on Ray: stage what the job reads, submit the driver, read what it wrote.

The driver is ray_core in the `ray_training` package (ray-training/ray_core), with the task's bundle on the path. It runs on the CPU pool (no GPU of its own: the
GPU belongs to the Ray task it starts, docs/trainer-plan.md "Ray execution") and gets the `ingest` credential
(staging bucket only), a spec of presigned reads, and no lakeFS or MLflow credential. Everything it writes goes under
the run's prefix (layout.train_prefix); core-api reads report.json and publishes the model (mlflow.publish_run_model).
"""
import asyncio
import json

from botocore.exceptions import ClientError

from app.core import secret_files
from app.core.enums import JobPackageName
from app.core.settings import settings
from app.core.task_catalog import DataKind
from app.integrations.data_plane import access, data_kinds, job_catalog, job_package, job_submit, lakefs, layout
from app.integrations.data_plane.train_report import TrainReport
from app.integrations.job_spec import TrainData, TrainSpec

_S3_NO_SUCH_KEY = "NoSuchKey"
INDEX_PATH_COLUMN = "path"


async def prepare_train_data(
    tenant_id: str, project_id: str, dataset_id: str, version_id: str, run_id: str, ttl_seconds: int, *, data_kind: DataKind = DataKind.IMAGES
) -> TrainData:
    """Presign the version's index and every image it lists, stage {index path: url} as the run's manifest.json, presign the
    manifest. A table dataset (tabular, text) has one file, data.parquet, and needs only that read. All reads stay valid for
    `ttl_seconds`, which must outlast the run's queue time and `max_runtime_s`."""
    commit = await access.version_commit(tenant_id, dataset_id, version_id)
    kind_layout = data_kinds.LAYOUTS[data_kind]
    if not kind_layout.has_objects:
        urls = await lakefs.presign_version_objects(tenant_id, dataset_id, version_id, [kind_layout.data_key], ttl_seconds)
        return TrainData(data_url=urls[kind_layout.data_key], version_id=version_id, lakefs_commit=commit)
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


async def submit_train(
    tenant_id: str, project_id: str, run_id: str, spec: TrainSpec, package_sha256: str, events_token: str | None = None,
    *, bundle_name: str | None = None, requirements: list[str] | None = None,
) -> str:
    """Submit the driver under the spec's job_id (the caller's submission id), running the ray_training package the run
    was created with and, for a task trained by a bundle (`bundle_name`), the bundle the spec pins next to it; returns the
    Ray job id. `requirements` are the exact pins Ray installs for the run (core's and the bundle's, merged). `events_token` goes into the job's environment under the name spec.reporting.token_env, never into the spec."""
    await access.training_run(tenant_id, project_id, run_id)
    package_url = await job_package.presigned_url(JobPackageName.RAY_TRAINING, package_sha256)
    bundle_urls = [await job_package.presigned_url(bundle_name, spec.payload.bundle_sha256)] if bundle_name else []
    module = job_catalog.TRAIN_ENTRYPOINT if bundle_name else job_catalog.LEGACY_TRAIN_ENTRYPOINT
    # num_gpus stays 0: the driver must not hold a GPU, the Ray Train workers it starts ask for them (spec.resources.gpu)
    return await job_submit.submit(
        package_url=package_url, bundle_urls=bundle_urls, module=module, spec_json=spec.model_dump_json(), job_id=spec.job_id,
        metadata={"tenant_id": tenant_id, "project_id": project_id, "training_run_id": run_id},
        num_cpus=settings.training.driver_cpus, timeout_seconds=spec.resources.max_runtime_s, requirements=requirements,
        extra_env={spec.reporting.token_env: events_token} if spec.reporting and events_token else None,
    )


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


async def purge_train_prefix(tenant_id: str, project_id: str, run_id: str) -> None:
    """Delete everything under the run's prefix once the run has ended and its model is in MLflow: the manifest holds
    presigned reads of the customer's data, and nothing reads the prefix after the settle."""
    await access.training_run(tenant_id, project_id, run_id)
    prefix = layout.train_prefix(tenant_id, project_id, run_id)
    await asyncio.to_thread(lakefs._delete_prefix, settings.data_plane.staging_bucket, prefix, lakefs.s3_client(secret_files.STAGING))


async def read_model_meta(tenant_id: str, project_id: str, run_id: str) -> dict | None:
    """The job's model.json (what publish_run_model copies into MLflow), or None when the run has none."""
    await access.training_run(tenant_id, project_id, run_id)
    key = layout.train_prefix(tenant_id, project_id, run_id) + layout.MODEL_META_KEY
    try:
        obj = await asyncio.to_thread(lakefs.s3_client(secret_files.STAGING).get_object, Bucket=settings.data_plane.staging_bucket, Key=key)
    except ClientError as e:
        if e.response["Error"]["Code"] == _S3_NO_SUCH_KEY:
            return None
        raise
    return json.loads(await asyncio.to_thread(obj["Body"].read))
