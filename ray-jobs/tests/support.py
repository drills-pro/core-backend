"""Shared fixtures-as-functions for the job tests. No Ray, MinIO or PIL; pyarrow only where a test says so."""
import io
import zipfile
from pathlib import Path

from ray_jobs.common.constants import SYMLINK_MODE, TABULAR_CLASSIFICATION
from ray_jobs.common.limits import Limits
from ray_jobs.common.report import Report
from ray_jobs.sources.csv_table import build_table, read_csv_text

LIMITS = Limits(
    upload_max_bytes=10_000, extracted_max_bytes=10_000, extracted_max_ratio=100, max_files=50, max_archive_depth=3,
    max_path_length=60, image_max_dimension=100, image_max_bytes=1_000, csv_min_rows=2, csv_max_rows=100,
    csv_max_columns=5, hf_max_repo_bytes=1_000, job_timeout_seconds=30,
)
JPG = b"\xff\xd8\xff" + b"x" * 20
PNG = b"\x89PNG" + b"y" * 20


def fake_decode(data: bytes) -> tuple[int, int]:
    if data.startswith(b"BAD"):
        raise ValueError("corrupt")
    return (200, 10) if data.startswith(b"BIG") else (10, 10)


def make_zip(files: dict[str, bytes], symlinks: tuple[str, ...] = ()) -> zipfile.ZipFile:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
        for name in symlinks:
            info = zipfile.ZipInfo(name)
            info.external_attr = (SYMLINK_MODE | 0o777) << 16
            zf.writestr(info, "target")
    return zipfile.ZipFile(io.BytesIO(buf.getvalue()))


def zip_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def codes(report: Report) -> list[str]:
    return [i["code"] for i in report.issues]


class FakeStore:
    def __init__(self, upload: bytes | None = None):
        self.upload, self.puts = upload, {}

    def size(self, key):
        return len(self.upload)

    def download(self, key, path: Path):
        path.write_bytes(self.upload)

    def put(self, key, data):
        self.puts[key] = data


def params(task, **extra):
    return {"source": "upload", "task": task, "limits": LIMITS.__dict__, **extra}


def table(csv_text, task=TABULAR_CLASSIFICATION, target="y", split=None):
    rep = Report()
    header, grid = read_csv_text(csv_text.encode())
    return build_table(header, grid, task, target, split, LIMITS, rep), rep
