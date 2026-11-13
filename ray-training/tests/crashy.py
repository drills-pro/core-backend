"""A trainer for test_driver: tests.tiny that loses its worker once, right after the first epoch's checkpoint."""
import tempfile
from pathlib import Path

from ray_core import loop
from tests import tiny


def crash_marker(job_id: str) -> Path:
    return Path(tempfile.gettempdir()) / f"crashy-{job_id}"


def train(spec, work, get):
    marker = crash_marker(spec.job_id)
    real = loop.session.report

    def report_then_crash(metrics, state):
        real(metrics, state)
        if loop.session.rank == 0 and not marker.exists():
            marker.write_text("crashed")
            raise RuntimeError("worker lost")

    loop.session.report = report_then_crash
    return tiny.train(spec, work, get)
