"""Smoke job: the CSV upload job end to end on a tiny CSV, to check a package before it is promoted.

    python -m ray_jobs.entrypoints.smoke '{"job_id": ..., "bucket": ..., "prefix": ...}'      # STAGING_* in the environment

Writes smoke.json ({"ok": bool}) under the prefix; exit code 1 when the import failed.
"""
import json
import os
import sys
from dataclasses import asdict

from ray_jobs import runner
from ray_jobs.common.constants import SMOKE_KEY, SPEC_VERSION, TABULAR_CLASSIFICATION, UPLOAD_KEY
from ray_jobs.common.enums import JobKind, Param
from ray_jobs.common.limits import Limits
from ray_jobs.common.store import S3Store
from ray_jobs.entrypoints import upload_csv

CSV = b"x,y\n1,a\n2,b\n3,a\n4,b\n"
LIMITS = Limits(
    upload_max_bytes=1 << 20, extracted_max_bytes=1 << 20, extracted_max_ratio=100, max_files=10, max_archive_depth=2,
    max_path_length=100, image_max_dimension=100, image_max_bytes=1 << 20, csv_min_rows=2, csv_max_rows=100,
    csv_max_columns=5, hf_max_repo_bytes=1 << 20, job_timeout_seconds=120)


def main(argv: list[str]) -> int:
    arg = json.loads(argv[1])
    store = S3Store(arg[Param.BUCKET], arg[Param.PREFIX], os.environ["STAGING_ENDPOINT"],
                    os.environ["STAGING_ACCESS_KEY"], os.environ["STAGING_SECRET_KEY"])
    store.put(UPLOAD_KEY, CSV)
    doc = {"spec_version": SPEC_VERSION, "kind": JobKind.INGEST, "job_id": arg["job_id"], "payload": {
        Param.BUCKET: arg[Param.BUCKET], Param.PREFIX: arg[Param.PREFIX], Param.SOURCE: "upload",
        Param.TASK: TABULAR_CLASSIFICATION, Param.TARGET: "y", Param.LIMITS: asdict(LIMITS)}}
    code = runner.main([argv[0], json.dumps(doc)], upload_csv.extract)
    store.put(SMOKE_KEY, json.dumps({"ok": code == 0}).encode())
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
