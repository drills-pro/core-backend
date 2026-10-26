"""Everything one extraction needs, built by runner.run."""
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ray_jobs.common.limits import Limits
from ray_jobs.common.report import Report
from ray_jobs.common.store import Sink


@dataclass
class Job:
    params: dict
    store: object  # S3Store, or a stand-in with size / download / put
    limits: Limits
    task: str
    report: Report
    decode: Callable[[bytes], tuple[int, int]]
    sink: Sink
    tmp: Path  # scratch directory, removed when the run ends
