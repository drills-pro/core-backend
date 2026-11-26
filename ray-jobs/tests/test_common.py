import io
import threading
import time

import pytest

from ray_jobs.common.constants import IMAGE_CLASSIFICATION, LABEL_COLUMN, MAX_REPORT_ISSUES, TABULAR_CLASSIFICATION, TIMEOUT_LIMIT
from ray_jobs.common.enums import BOX_FIELDS, INDEX_COLUMNS, Code
from ray_jobs.common.limits import LimitExceeded, Limits
from ray_jobs.common.report import Report
from ray_jobs.common.rows import image_row
from ray_jobs.common.stats import compute_stats
from ray_jobs.common.store import ParallelSink, http_get
from tests.support import JPG, LIMITS, PNG, table


def test_limits_ignore_extra_keys_and_require_every_knob():
    assert Limits.from_dict({**LIMITS.__dict__, "ray_cpus": 4}) == LIMITS
    with pytest.raises(KeyError):
        Limits.from_dict({k: v for k, v in LIMITS.__dict__.items() if k != "max_files"})


def test_timeout_key_is_a_limit():
    assert TIMEOUT_LIMIT in Limits.__dataclass_fields__


@pytest.mark.parametrize("url, status, expected", [
    ("https://huggingface.co/api/datasets/x/revision/main", 401, Code.HF_ACCESS_DENIED),
    ("https://huggingface.co/datasets/x/resolve/main/a.parquet", 403, Code.HF_ACCESS_DENIED),
    ("https://huggingface.co/api/datasets/x/revision/main", 404, None),
    ("https://bucket.example/presigned?sig=1", 403, None),
])
def test_http_get_names_only_a_hugging_face_refusal(monkeypatch, url, status, expected):
    import urllib.request
    from urllib.error import HTTPError

    def refuse(u, timeout):
        raise HTTPError(u, status, "refused", None, None)

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    if expected is None:
        with pytest.raises(HTTPError):
            http_get(url)
    else:
        with pytest.raises(LimitExceeded) as e:
            http_get(url)
        assert e.value.code == expected


def test_report_caps_issues_and_counts_the_rest():
    rep = Report()
    for _ in range(MAX_REPORT_ISSUES + 5):
        rep.error(Code.CORRUPT_IMAGE, "x")
    out = rep.to_dict()
    assert len(out["issues"]) == MAX_REPORT_ISSUES and out["issues_omitted"] == 5


def test_a_failed_report_has_no_stats():
    rep = Report()
    rep.error(Code.NO_DATA, "x")
    assert rep.to_dict({"row_count": 1})["stats"] is None


def test_image_rows_use_the_layout_column_order():
    assert tuple(image_row("a/1.jpg", JPG, "a", [], None)) == INDEX_COLUMNS and BOX_FIELDS[0] == "category"


def test_stats():
    rows = [image_row("a/1.jpg", JPG, "a", None, "train"), image_row("b/2.png", PNG, "b", None, None)]
    s = compute_stats(rows, IMAGE_CLASSIFICATION, None, 2)
    assert s == {"file_count": 2, "total_bytes": len(JPG) + len(PNG), "row_count": 2,
                 "classes": {"a": 1, "b": 1}, "splits": {"train": 1}, "duplicates_skipped": 2}
    t, _ = table("a,y\n1,x\n2,x\n3,z\n")
    assert compute_stats(t.rows, TABULAR_CLASSIFICATION, LABEL_COLUMN, 0)["classes"] == {"x": 2, "z": 1}


def test_http_get_stops_reading_at_the_cap(monkeypatch):
    class Resp(io.BytesIO):
        def __enter__(self):
            return self

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout: Resp(b"x" * 50))
    assert http_get("u", 50) == b"x" * 50
    with pytest.raises(LimitExceeded):
        http_get("u", 49)


def test_stats_and_ingest_refuse_a_task_they_do_not_know():
    with pytest.raises(KeyError):
        compute_stats([], "image-segmentation", None, 0)


def test_parallel_sink_uploads_every_object_before_it_returns():
    got = {}
    with ParallelSink(lambda path, data: got.__setitem__(path, data), threads=4, max_pending=8) as sink:
        for i in range(50):
            sink(f"o/{i}", bytes([i]))
    assert got == {f"o/{i}": bytes([i]) for i in range(50)}


def test_parallel_sink_raises_the_first_failure_on_exit():
    def put(path, data):
        if path == "o/3":
            raise OSError("store down")

    with pytest.raises(OSError, match="store down"):
        with ParallelSink(put, threads=2, max_pending=4) as sink:
            for i in range(6):
                sink(f"o/{i}", b"x")


def test_parallel_sink_stops_the_extraction_after_a_failure():
    def put(path, data):
        raise OSError("store down")

    with pytest.raises(OSError):
        with ParallelSink(put, threads=1, max_pending=2) as sink:
            for _ in range(500):  # the failure is recorded just after put raises: a later call raises it, without waiting for the exit
                sink("o/x", b"x")
                time.sleep(0.01)
            pytest.fail("the failure never reached the caller")


def test_parallel_sink_holds_at_most_max_pending_uploads():
    release, running, peak, lock = threading.Event(), 0, 0, threading.Lock()

    def put(path, data):
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        release.wait(5)
        with lock:
            running -= 1

    with ParallelSink(put, threads=8, max_pending=3) as sink:
        feeder = threading.Thread(target=lambda: [sink(f"o/{i}", b"x") for i in range(20)])
        feeder.start()
        threading.Timer(0.3, release.set).start()  # the feeder blocks on the 4th image until uploads finish
        feeder.join(10)
    assert peak <= 3
