"""The one run() every job type goes through, so none can skip report.json.

An entrypoint supplies only `extract(job) -> Extracted` (read its source into rows or a table); merging with
the base, writing out/ and report.json, stats, and the time limit are done here.
"""
import json
import os
import signal
import tempfile
from pathlib import Path
from typing import Callable

from ray_jobs.common.constants import (
    DATA_KEY, IMAGE_TASKS, INDEX_KEY, NO_ALARM, OBJECTS_PREFIX, OUT_PREFIX, REPORT_KEY, SCHEMA_KEY, TIMEOUT_LIMIT)
from ray_jobs.common.enums import Code, Param
from ray_jobs.common.job import Job
from ray_jobs.common.limits import LimitExceeded, Limits
from ray_jobs.common.parquet import index_parquet, table_parquet
from ray_jobs.common.report import Report
from ray_jobs.common.stats import compute_stats
from ray_jobs.common.store import S3Store
from ray_jobs.common.table import Extracted
from ray_jobs.spec import parse_spec
from ray_jobs.merge import load_base, merge_index, merge_tables
from ray_jobs.sources.images import pil_decode

Extract = Callable[[Job], Extracted]


def run(params: dict, store, extract: Extract, decode=pil_decode) -> Report:
    """Do the import and write report.json (always). Returns the Report."""
    report = Report()
    stats = None
    revision = license_ = None
    try:
        limits, task = Limits.from_dict(params[Param.LIMITS]), params[Param.TASK]  # inside the try: bad params still get a report
        base_rows, base_schema = load_base(params, report)
        skipped = 0
        def sink(path, data):
            return store.put(OUT_PREFIX + OBJECTS_PREFIX + path, data)

        with tempfile.TemporaryDirectory() as tmp:
            got = extract(Job(params, store, limits, task, report, decode, sink, Path(tmp)))
        out_rows, target, table, revision, license_ = got.rows, got.target, got.table, got.revision, got.license
        if not report.failed:
            if task in IMAGE_TASKS:
                out_rows, skipped = merge_index(base_rows or [], out_rows or [], report)
            elif table is not None:
                out_rows = merge_tables(base_schema, base_rows or [], table, report)
        if not report.failed:
            if task in IMAGE_TASKS:
                store.put(OUT_PREFIX + INDEX_KEY, index_parquet(out_rows))
            else:
                store.put(OUT_PREFIX + DATA_KEY, table_parquet(out_rows, table))
                store.put(OUT_PREFIX + SCHEMA_KEY, json.dumps(table.schema).encode())
            stats = compute_stats(out_rows, task, target, skipped)
    except LimitExceeded as e:
        report.error(e.code, e.message)
    except TimeoutError:
        report.error(Code.JOB_TIMEOUT, "job exceeded its time limit")
    except Exception as e:  # noqa: BLE001 - the report must always be written
        report.error(Code.INTERNAL_ERROR, f"{type(e).__name__}: {e}")
    finally:
        signal.alarm(NO_ALARM)  # the job timeout must not fire inside the report write below
    store.put(REPORT_KEY, json.dumps(report.to_dict(stats, revision, license_)).encode())
    return report


def main(argv: list[str], extract: Extract) -> int:
    """Entry of every job type: `python -m ray_jobs.entrypoints.<name> '<job spec json>'`, staging credentials in the
    environment. Exit code 1 when the report is failed."""
    params = parse_spec(argv[1])
    store = S3Store(params[Param.BUCKET], params[Param.PREFIX], os.environ["STAGING_ENDPOINT"],
                    os.environ["STAGING_ACCESS_KEY"], os.environ["STAGING_SECRET_KEY"])

    def on_alarm(*_):
        raise TimeoutError

    signal.signal(signal.SIGALRM, on_alarm)
    signal.alarm(params.get(Param.LIMITS, {}).get(TIMEOUT_LIMIT, NO_ALARM))  # run() reports bad limits
    try:
        return 1 if run(params, store, extract).failed else 0
    finally:
        signal.alarm(NO_ALARM)
