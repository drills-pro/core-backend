"""The staging bucket (S3, ray_common.store) and plain HTTP reads, plus the parallel uploader of the ingest job."""
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError

from ray_common.constants import CHECKSUM_ENV, CHECKSUM_WHEN_REQUIRED  # noqa: F401 - re-exported for callers and drift tests
from ray_common.store import ResponseTooLarge, S3Store  # noqa: F401 - S3Store is re-exported: ray_jobs callers import it from here
from ray_common.store import http_get as _http_get
from ray_jobs.common.constants import HF_ACCESS_STATUSES, HF_ORIGIN, UPLOAD_KEY, UPLOAD_MAX_PENDING, UPLOAD_THREADS
from ray_jobs.common.enums import Code
from ray_jobs.common.limits import LimitExceeded

Sink = Callable[[str, bytes], None]


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
    try:
        return _http_get(url, max_bytes)
    except ResponseTooLarge:
        raise LimitExceeded(Code.HF_TOO_LARGE, f"a download is over {max_bytes} bytes") from None
    except HTTPError as e:  # only the Hub's refusals are named; a presigned URL's 403 stays a plain error
        if url.startswith(HF_ORIGIN) and e.code in HF_ACCESS_STATUSES:
            raise LimitExceeded(Code.HF_ACCESS_DENIED, f"Hugging Face answered {e.code}: the repo is gated or private, and imports send no token") from None
        raise


def fetch_upload(store, upload_max_bytes: int, tmp: Path) -> Path:
    """Download the uploaded archive or CSV; an oversized one is refused before it fills the disk."""
    if (uploaded := store.size(UPLOAD_KEY)) > upload_max_bytes:
        raise LimitExceeded(Code.ARCHIVE_TOO_LARGE, f"upload is {uploaded} bytes, limit {upload_max_bytes}")
    source = tmp / "source"
    store.download(UPLOAD_KEY, source)
    return source
