"""The staging bucket (S3) and plain HTTP reads. boto3 is imported lazily."""
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from ray_jobs.common.constants import HTTP_TIMEOUT_SECONDS, UPLOAD_KEY, UPLOAD_MAX_PENDING, UPLOAD_THREADS
from ray_jobs.common.enums import Code
from ray_jobs.common.limits import LimitExceeded

Sink = Callable[[str, bytes], None]


class S3Store:
    """Staging bucket under one prefix. boto3 imported lazily."""

    def __init__(self, bucket: str, prefix: str, endpoint: str, access_key: str, secret_key: str):
        import boto3

        self.bucket, self.prefix = bucket, prefix
        self.client = boto3.client("s3", endpoint_url=endpoint, aws_access_key_id=access_key, aws_secret_access_key=secret_key)

    def size(self, key: str) -> int:
        return self.client.head_object(Bucket=self.bucket, Key=self.prefix + key)["ContentLength"]

    def download(self, key: str, path: Path) -> None:
        self.client.download_file(self.bucket, self.prefix + key, str(path))

    def put(self, key: str, data: bytes) -> None:
        self.client.put_object(Bucket=self.bucket, Key=self.prefix + key, Body=data)


class ParallelSink:
    """A Sink that uploads on a thread pool. Use it as a context manager: leaving it waits for every upload and raises the first
    failure, so nothing written after it (index.parquet, report.json) can precede an object it names. A failure also surfaces on
    the next call, so a broken store stops the extraction early. At most `max_pending` images are held in memory at once."""

    def __init__(self, put: Sink, threads: int = UPLOAD_THREADS, max_pending: int = UPLOAD_MAX_PENDING):
        self._put = put
        self._pool = ThreadPoolExecutor(threads)
        self._slots = threading.Semaphore(max_pending)
        self._error: BaseException | None = None

    def __call__(self, path: str, data: bytes) -> None:
        self._raise()
        self._slots.acquire()
        self._pool.submit(self._upload, path, data)

    def _upload(self, path: str, data: bytes) -> None:
        try:
            self._put(path, data)
        except BaseException as e:  # noqa: BLE001 - re-raised on the caller's thread by _raise
            self._error = self._error or e
        finally:
            self._slots.release()

    def _raise(self) -> None:
        if self._error is not None:
            raise self._error

    def __enter__(self) -> "ParallelSink":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._pool.shutdown(wait=True)
        if exc is None:
            self._raise()


def http_get(url: str, max_bytes: int | None = None) -> bytes:
    """GET a presigned or huggingface.co URL; with max_bytes, stop reading one byte past the cap."""
    from urllib.request import urlopen

    with urlopen(url, timeout=HTTP_TIMEOUT_SECONDS) as resp:  # noqa: S310 - presigned or huggingface.co URL built by this job
        data = resp.read(None if max_bytes is None else max_bytes + 1)
    if max_bytes is not None and len(data) > max_bytes:
        raise LimitExceeded(Code.HF_TOO_LARGE, f"a download is over {max_bytes} bytes")
    return data


def fetch_upload(store, upload_max_bytes: int, tmp: Path) -> Path:
    """Download the uploaded archive or CSV; an oversized one is refused before it fills the disk."""
    if (uploaded := store.size(UPLOAD_KEY)) > upload_max_bytes:
        raise LimitExceeded(Code.ARCHIVE_TOO_LARGE, f"upload is {uploaded} bytes, limit {upload_max_bytes}")
    source = tmp / "source"
    store.download(UPLOAD_KEY, source)
    return source
