"""Ray jobs of the dataset import (docs/datasets-plan.md step 7). One entrypoint per job type:

    python -m ray_jobs.entrypoints.upload_images '<params json>'   zip of images (classification folders or COCO)
    python -m ray_jobs.entrypoints.upload_csv '<params json>'      CSV, tabular tasks
    python -m ray_jobs.entrypoints.huggingface '<params json>'     Hugging Face dataset, any task

with STAGING_ENDPOINT / STAGING_ACCESS_KEY / STAGING_SECRET_KEY in the environment: the job holds no lakeFS
credential and writes only under params["prefix"]. params: {bucket, prefix, source, task, target?, split?,
limits{...DatasetLimits}, base?: {index_url|data_url, schema_url?}, hf?: {repo, revision, column?}}.

No `app.*` imports. The contract with core-api is the staging layout (app/integrations/data_plane/layout.py, mirrored
in common/constants.py) and the report schema (report.schema.json); core-api's tests/ingest_job/test_contract.py
asserts both. boto3, pyarrow and PIL are imported lazily at the I/O edges so the pure logic tests without them.

The job writes `out/objects/<path>` for NEW images only plus the merged index: commit_staged imports `out/` onto
main, which keeps the base version's objects already in the repo. Image index `path` is relative to objects/.
A failed run can leave `out/objects/*` in staging next to a failed report.json and no index; that is harmless,
commit_staged only runs for an `ok` report and discard / expiry purge the whole prefix.
"""
