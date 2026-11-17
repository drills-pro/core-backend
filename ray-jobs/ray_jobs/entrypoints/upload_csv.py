"""Job type: a CSV upload, tabular-classification or tabular-regression.

    python -m ray_jobs.entrypoints.upload_csv '<params json>'
"""
import sys

from ray_jobs.common.constants import LABEL_COLUMN, TABULAR_TASKS
from ray_jobs.common.job import Job
from ray_jobs.common.enums import Param
from ray_jobs.common.store import fetch_upload
from ray_jobs.common.table import Extracted
from ray_jobs.runner import main
from ray_jobs.sources.csv_table import read_csv_upload


def extract(job: Job) -> Extracted:
    source = fetch_upload(job.store, job.limits.upload_max_bytes, job.tmp)
    target = job.params.get(Param.TARGET)
    table = read_csv_upload(source, job.task, target, job.params.get(Param.SPLIT), job.limits, job.report)
    return Extracted(None, LABEL_COLUMN if job.task in TABULAR_TASKS else target, table)


if __name__ == "__main__":
    sys.exit(main(sys.argv, extract))
