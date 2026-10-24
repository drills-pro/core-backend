"""Job type: a zip of images, image-classification (`<class>/<image>`) or object-detection (COCO).

    python -m ray_jobs.entrypoints.upload_images '<params json>'
"""
import sys
import zipfile

from ray_jobs.common.constants import IMAGE_CLASSIFICATION
from ray_jobs.common.enums import Code, Param
from ray_jobs.common.job import Job
from ray_jobs.common.store import fetch_upload
from ray_jobs.common.table import Extracted
from ray_jobs.runner import main
from ray_jobs.sources.archive import inspect_archive
from ray_jobs.sources.coco import ingest_detection
from ray_jobs.sources.images import ingest_classification


def extract(job: Job) -> Extracted:
    source = fetch_upload(job.store, job.limits.upload_max_bytes, job.tmp)
    try:
        zf = zipfile.ZipFile(source)
    except zipfile.BadZipFile:
        job.report.error(Code.NO_DATA, "upload is not a valid zip archive")
        return Extracted([], None, None)
    with zf:
        members = inspect_archive(zf.infolist(), source.stat().st_size, job.limits, job.report)
        if job.report.failed:
            return Extracted([], None, None)
        ingest = ingest_classification if job.task == IMAGE_CLASSIFICATION else ingest_detection
        rows = ingest(zf, members, job.limits, job.report, job.decode, job.sink, job.params.get(Param.SPLIT))
        return Extracted(rows, None, None)


if __name__ == "__main__":
    sys.exit(main(sys.argv, extract))
