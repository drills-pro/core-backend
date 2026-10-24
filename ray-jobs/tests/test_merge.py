from ray_jobs.common.constants import IMAGE_CLASSIFICATION, OBJECT_DETECTION
from ray_jobs.common.enums import Code
from ray_jobs.common.report import Report
from ray_jobs.common.rows import image_row
from ray_jobs.common.stats import compute_stats
from ray_jobs.merge import merge_index, merge_tables
from tests.support import JPG, PNG, codes, table


def test_merge_skips_exact_duplicates_and_adds_new():
    base = [image_row("a/1.jpg", JPG, "a", None, None)]
    new = [image_row("a/1.jpg", JPG, "a", None, None), image_row("a/2.jpg", PNG, "a", None, None)]
    rep = Report()
    merged, skipped = merge_index(base, new, rep)
    assert [r["path"] for r in merged] == ["a/1.jpg", "a/2.jpg"] and skipped == 1 and not rep.failed
    assert codes(rep) == [Code.DUPLICATE_SKIPPED]


def test_merge_conflict_same_path_other_bytes():
    rep = Report()
    merge_index([image_row("a/1.jpg", JPG, "a", None, None)], [image_row("a/1.jpg", PNG, "a", None, None)], rep)
    assert codes(rep) == [Code.MERGE_CONFLICT] and rep.failed


def test_merge_detects_label_case_clash_with_base_and_merges_categories_by_name():
    box = lambda c: [{"category": c, "x": 0, "y": 0, "width": 1, "height": 1}]  # noqa: E731
    rep = Report()
    merge_index([image_row("1.jpg", JPG, None, box("cat"), None)], [image_row("2.jpg", PNG, None, box("Cat"), None)], rep)
    assert Code.LABEL_CASE_COLLISION in codes(rep)
    rep = Report()
    merged, _ = merge_index([image_row("1.jpg", JPG, None, box("cat"), None)], [image_row("2.jpg", PNG, None, box("cat"), None)], rep)
    assert not rep.failed and compute_stats(merged, OBJECT_DETECTION, None, 0)["classes"] == {"cat": 2}


def test_csv_merge_requires_identical_schema():
    t, _ = table("a,y\n1,x\n2,z\n")
    same, rep = {"columns": t.schema["columns"]}, Report()
    assert merge_tables(same, [{"a": 0, "y": "q", "split": None}], t, rep) is not None and len(rep.issues) == 0
    rep = Report()
    other = {"columns": [{"name": "a", "type": "float64"}, {"name": "y", "type": "string"}]}
    assert merge_tables(other, [], t, rep) is None and codes(rep) == [Code.SCHEMA_MISMATCH]
