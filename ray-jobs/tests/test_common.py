import io

import pytest

from ray_jobs.common.constants import IMAGE_CLASSIFICATION, MAX_REPORT_ISSUES, TABULAR_CLASSIFICATION, TIMEOUT_LIMIT
from ray_jobs.common.enums import BOX_FIELDS, INDEX_COLUMNS, Code
from ray_jobs.common.limits import LimitExceeded, Limits
from ray_jobs.common.report import Report
from ray_jobs.common.rows import image_row
from ray_jobs.common.stats import compute_stats
from ray_jobs.common.store import http_get
from tests.support import JPG, LIMITS, PNG, table


def test_limits_ignore_extra_keys_and_require_every_knob():
    assert Limits.from_dict({**LIMITS.__dict__, "ray_cpus": 4}) == LIMITS
    with pytest.raises(KeyError):
        Limits.from_dict({k: v for k, v in LIMITS.__dict__.items() if k != "max_files"})


def test_timeout_key_is_a_limit():
    assert TIMEOUT_LIMIT in Limits.__dataclass_fields__


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
    assert compute_stats(t.rows, TABULAR_CLASSIFICATION, "y", 0)["classes"] == {"x": 2, "z": 1}


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
