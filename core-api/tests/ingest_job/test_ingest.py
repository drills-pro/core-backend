"""Pure-function tests for the standalone ingest job (docs/datasets-plan.md step 7). No Ray, MinIO, pyarrow or PIL."""
import io
import json
import re
import zipfile
from pathlib import Path

import pytest

from app.core.enums import TaskType
from app.modules.user.datasets.constants import ImportSource
from app.integrations.data_plane import layout, report as report_model
from ingest_job import ingest as job
from ingest_job.ingest import Code, Limits, Report

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
            info.external_attr = (job.SYMLINK_MODE | 0o777) << 16
            zf.writestr(info, "target")
    return zipfile.ZipFile(io.BytesIO(buf.getvalue()))


def codes(report: Report) -> list[str]:
    return [i["code"] for i in report.issues]


def classify(files, **kw):
    zf, rep, objects = make_zip(files, **kw), Report(), {}
    members = job.inspect_archive(zf.infolist(), sum(len(v) for v in files.values()) or 1, LIMITS, rep)
    rows = job.ingest_classification(zf, members, LIMITS, rep, fake_decode, objects.__setitem__, None)
    return rows, rep, objects


# ---- contract drift ----

def test_constants_mirror_the_layout_and_task_types():
    assert (job.UPLOAD_KEY, job.REPORT_KEY, job.OUT_PREFIX, job.OBJECTS_PREFIX) == (
        layout.UPLOAD_KEY, layout.REPORT_KEY, layout.OUT_PREFIX, layout.OBJECTS_PREFIX)
    assert (job.INDEX_KEY, job.DATA_KEY, job.SCHEMA_KEY) == (layout.INDEX_KEY, layout.DATA_KEY, layout.SCHEMA_KEY)
    assert job.INDEX_COLUMNS == layout.INDEX_COLUMNS and job.SPLITS == tuple(s.value for s in layout.Split)
    assert (job.IMAGE_CLASSIFICATION, job.OBJECT_DETECTION) == (TaskType.IMAGE_CLASSIFICATION, TaskType.OBJECT_DETECTION)
    assert (job.TABULAR_CLASSIFICATION, job.TABULAR_REGRESSION) == (TaskType.TABULAR_CLASSIFICATION, TaskType.TABULAR_REGRESSION)
    assert job.REPORT_SCHEMA_VERSION == report_model.REPORT_SCHEMA_VERSION and job.MAX_REPORT_ISSUES == report_model.MAX_REPORT_ISSUES


def test_every_job_issue_code_is_in_the_report_schema_and_a_report_validates():
    assert {c.value for c in Code} <= {c.value for c in report_model.IssueCode}
    rep = Report()
    rep.warn(Code.DUPLICATE_SKIPPED, "dup", "a")
    stats = job.compute_stats([job.image_row("a/1.jpg", JPG, "a", None, "train")], job.IMAGE_CLASSIFICATION, None, 1)
    parsed = report_model.ImportReport.model_validate(rep.to_dict(stats, "abc", "mit"))
    assert parsed.outcome == "ok" and parsed.stats.classes == {"a": 1}
    rep.error(Code.NO_DATA, "x")
    assert report_model.ImportReport.model_validate(rep.to_dict(stats)).stats is None


def test_enums_mirror_core_api():
    assert {m.value for m in job.Source} == {m.value for m in ImportSource}
    assert {m.value for m in job.Outcome} == {m.value for m in report_model.Outcome}
    assert {m.value for m in job.ColumnType} == {m.value for m in layout.ColumnType}


def test_limits_ignore_extra_keys_and_require_every_knob():
    assert Limits.from_dict({**LIMITS.__dict__, "ray_cpus": 4}) == LIMITS
    with pytest.raises(KeyError):
        Limits.from_dict({k: v for k, v in LIMITS.__dict__.items() if k != "max_files"})


def test_timeout_key_is_a_limit():
    assert job.TIMEOUT_LIMIT in Limits.__dataclass_fields__


def test_limits_cover_the_settings_knobs():
    from app.core.settings import DatasetLimits

    assert {f for f in Limits.__dataclass_fields__} <= set(DatasetLimits.model_fields)


def test_report_caps_issues_and_counts_the_rest():
    rep = Report()
    for _ in range(job.MAX_REPORT_ISSUES + 5):
        rep.error(Code.CORRUPT_IMAGE, "x")
    out = rep.to_dict()
    assert len(out["issues"]) == job.MAX_REPORT_ISSUES and out["issues_omitted"] == 5
    report_model.ImportReport.model_validate(out)


# ---- image classification ----

def test_valid_classification_zip():
    rows, rep, objects = classify({"cat/1.jpg": JPG, "dog/2.png": PNG, "__MACOSX/cat/._1.jpg": b"junk"})
    assert not rep.failed
    assert [(r["path"], r["label"], r["size"]) for r in rows] == [("cat/1.jpg", "cat", len(JPG)), ("dog/2.png", "dog", len(PNG))]
    assert set(objects) == {"cat/1.jpg", "dog/2.png"}


def test_zip_bomb_by_ratio_and_by_size():
    zf = make_zip({"a/1.jpg": b"\0" * 5000})
    rep = Report()
    job.inspect_archive(zf.infolist(), 20, LIMITS, rep)  # 5000 / 20 = 250 > 100
    assert Code.ARCHIVE_TOO_LARGE in codes(rep)
    rep = Report()
    job.inspect_archive(zf.infolist(), 10_000, Limits.from_dict({**LIMITS.__dict__, "extracted_max_bytes": 100}), rep)
    assert Code.ARCHIVE_TOO_LARGE in codes(rep)


def test_decompression_is_bounded_even_if_the_header_lies():
    zf = make_zip({"a/1.jpg": b"\0" * 5000})
    with pytest.raises(job.LimitExceeded):
        job.ByteBudget(100).read(zf, zf.infolist()[0])


@pytest.mark.parametrize("name", ["../evil.jpg", "/abs/x.jpg", "a/../../x.jpg", "C:/x.jpg", "a\\b.jpg"])
def test_path_traversal_is_rejected(name):
    rep = Report()
    infos = [zipfile.ZipInfo(name)]
    assert job.inspect_archive(infos, 100, LIMITS, rep) == []
    assert codes(rep) == [Code.ARCHIVE_UNSAFE_PATH]


def test_symlink_is_rejected():
    rows, rep, _ = classify({"cat/1.jpg": JPG}, symlinks=("cat/link.jpg",))
    assert Code.ARCHIVE_SYMLINK in codes(rep) and rep.failed


def test_depth_path_length_and_file_count_limits():
    rep = Report()
    deep = zipfile.ZipInfo("a/b/c/d/e/f.jpg")
    long = zipfile.ZipInfo("a/" + "x" * 80 + ".jpg")
    job.inspect_archive([deep, long], 100, LIMITS, rep)
    assert set(codes(rep)) == {Code.ARCHIVE_TOO_DEEP, Code.PATH_TOO_LONG}
    rep = Report()
    job.inspect_archive([zipfile.ZipInfo(f"a/{i}.jpg") for i in range(51)], 100, LIMITS, rep)
    assert Code.ARCHIVE_TOO_MANY_FILES in codes(rep)


def test_corrupt_and_oversized_images_fail():
    _, rep, _ = classify({"a/1.jpg": b"BAD" + JPG, "a/2.jpg": b"BIG" + JPG, "a/3.jpg": b"z" * 2000})
    assert sorted(codes(rep)) == sorted([Code.CORRUPT_IMAGE, Code.IMAGE_TOO_LARGE, Code.IMAGE_TOO_LARGE])


def test_wrong_label_case_is_a_conflict():
    _, rep, _ = classify({"Cat/1.jpg": JPG, "cat/2.jpg": PNG})
    assert Code.LABEL_CASE_COLLISION in codes(rep)


def test_root_images_and_non_images():
    _, rep, _ = classify({"1.jpg": JPG, "cat/notes.txt": b"hi", "cat/2.jpg": PNG})
    assert Code.NO_DATA in codes(rep)
    assert [i["severity"] for i in rep.issues if i["code"] == Code.UNSUPPORTED_EXTENSION] == ["warning"]


# ---- COCO detection ----

def coco(**over):
    doc = {"images": [{"id": 1, "file_name": "img/1.jpg"}, {"id": 2, "file_name": "img/2.png"}],
           "categories": [{"id": 1, "name": "cat"}, {"id": 2, "name": "dog"}],
           "annotations": [{"id": 1, "image_id": 1, "category_id": 2, "bbox": [1, 2, 3, 4]}]}
    return {**doc, **over}


def detect(doc, files=None):
    files = {"data/annotations.json": json.dumps(doc).encode(), "data/img/1.jpg": JPG, "data/img/2.png": PNG, **(files or {})}
    zf, rep = make_zip(files), Report()
    rows = job.ingest_detection(zf, zf.infolist(), LIMITS, rep, fake_decode, lambda *_: None, "train")
    return rows, rep


def test_valid_coco():
    rows, rep = detect(coco())
    assert not rep.failed
    assert rows[0]["boxes"] == [{"category": "dog", "x": 1, "y": 2, "width": 3, "height": 4}] and rows[1]["boxes"] == []
    assert rows[0]["split"] == "train" and rows[0]["path"] == "data/img/1.jpg"


@pytest.mark.parametrize("doc", [
    coco(categories=[{"id": 1, "name": "cat"}, {"id": 1, "name": "dog"}]),   # category id clash
    coco(images=[{"id": 1, "file_name": "img/1.jpg"}, {"id": 1, "file_name": "img/2.png"}]),  # image id clash
    coco(categories=[{"id": 1, "name": "cat"}, {"id": 2, "name": "cat"}]),
    coco(annotations=[{"id": 1, "image_id": 9, "category_id": 1, "bbox": [1, 2, 3, 4]}]),
    coco(annotations=[{"id": 1, "image_id": 1, "category_id": 1, "bbox": [1, 2, 0, 4]}]),
    {"images": []},
])
def test_invalid_coco(doc):
    _, rep = detect(doc)
    assert Code.COCO_INVALID in codes(rep)


def test_coco_image_missing_from_archive_and_missing_json():
    _, rep = detect(coco(images=[{"id": 1, "file_name": "nope.jpg"}]))
    assert Code.COCO_INVALID in codes(rep)
    zf, rep = make_zip({"a.jpg": JPG}), Report()
    job.ingest_detection(zf, zf.infolist(), LIMITS, rep, fake_decode, lambda *_: None, None)
    assert Code.COCO_INVALID in codes(rep)


# ---- CSV ----

def table(csv_text, task=job.TABULAR_CLASSIFICATION, target="y", split=None):
    rep = Report()
    header, grid = job.read_csv_text(csv_text.encode())
    return job.build_table(header, grid, task, target, split, LIMITS, rep), rep


def test_csv_schema_inference_and_split_tag():
    t, rep = table("a,b,c,y\n1,1.5,x,yes\n2,2,y,no\n,3,z,yes\n", split="train")
    assert not rep.failed
    assert t.schema == {"columns": [{"name": "a", "type": "int64"}, {"name": "b", "type": "float64"},
                                    {"name": "c", "type": "string"}, {"name": "y", "type": "string"}]}
    assert t.rows[2]["a"] is None and t.rows[0]["split"] == "train" and t.rows[0]["b"] == 1.5


def test_csv_bool_and_int_columns():
    assert job.infer_type(["true", "False", ""]) == "bool"
    assert job.infer_type(["1", "2"]) == "int64" and job.infer_type(["1", "2.5"]) == "float64" and job.infer_type([""]) == "string"


@pytest.mark.parametrize("text,code", [
    ("", Code.CSV_NO_HEADER),
    ("a,,y\n1,2,3\n4,5,6\n", Code.CSV_NO_HEADER),
    ("a,a,y\n1,2,3\n4,5,6\n", Code.CSV_DUPLICATE_COLUMNS),
    ("a,split,y\n1,2,3\n4,5,6\n", Code.CSV_DUPLICATE_COLUMNS),
    ("a,b,c,d,e,y\n" + "1,1,1,1,1,1\n" * 3, Code.CSV_TOO_MANY_COLUMNS),
    ("a,y\n1,1\n", Code.CSV_TOO_FEW_ROWS),
    ("a,y\n" + "1,1\n" * 101, Code.CSV_TOO_MANY_ROWS),
    ("a,z\n1,1\n2,2\n", Code.TARGET_MISSING),
    ("a,y\n1,1\n2,\n", Code.TARGET_MISSING),
    ("a,y\n1,1.5\n2,2.5\n", Code.TARGET_TYPE_MISMATCH),  # float target cannot be a class
])
def test_csv_errors(text, code):
    t, rep = table(text)
    assert t is None and codes(rep) == [code]


def test_regression_target_must_be_numeric():
    t, rep = table("a,y\n1,x\n2,z\n", task=job.TABULAR_REGRESSION)
    assert t is None and codes(rep) == [Code.TARGET_TYPE_MISMATCH]
    t, rep = table("a,y\n1,1.5\n2,2\n", task=job.TABULAR_REGRESSION)
    assert t is not None and not rep.failed


def test_csv_merge_requires_identical_schema():
    t, _ = table("a,y\n1,x\n2,z\n")
    same, rep = {"columns": t.schema["columns"]}, Report()
    assert job.merge_tables(same, [{"a": 0, "y": "q", "split": None}], t, rep) is not None and len(rep.issues) == 0
    rep = Report()
    other = {"columns": [{"name": "a", "type": "float64"}, {"name": "y", "type": "string"}]}
    assert job.merge_tables(other, [], t, rep) is None and codes(rep) == [Code.SCHEMA_MISMATCH]


# ---- merge ----

def test_merge_skips_exact_duplicates_and_adds_new():
    base = [job.image_row("a/1.jpg", JPG, "a", None, None)]
    new = [job.image_row("a/1.jpg", JPG, "a", None, None), job.image_row("a/2.jpg", PNG, "a", None, None)]
    rep = Report()
    merged, skipped = job.merge_index(base, new, rep)
    assert [r["path"] for r in merged] == ["a/1.jpg", "a/2.jpg"] and skipped == 1 and not rep.failed
    assert codes(rep) == [Code.DUPLICATE_SKIPPED]


def test_merge_conflict_same_path_other_bytes():
    rep = Report()
    job.merge_index([job.image_row("a/1.jpg", JPG, "a", None, None)], [job.image_row("a/1.jpg", PNG, "a", None, None)], rep)
    assert codes(rep) == [Code.MERGE_CONFLICT] and rep.failed


def test_merge_detects_label_case_clash_with_base_and_merges_categories_by_name():
    box = lambda c: [{"category": c, "x": 0, "y": 0, "width": 1, "height": 1}]  # noqa: E731
    rep = Report()
    job.merge_index([job.image_row("1.jpg", JPG, None, box("cat"), None)], [job.image_row("2.jpg", PNG, None, box("Cat"), None)], rep)
    assert Code.LABEL_CASE_COLLISION in codes(rep)
    rep = Report()
    merged, _ = job.merge_index([job.image_row("1.jpg", JPG, None, box("cat"), None)], [job.image_row("2.jpg", PNG, None, box("cat"), None)], rep)
    assert not rep.failed and job.compute_stats(merged, job.OBJECT_DETECTION, None, 0)["classes"] == {"cat": 2}


def test_stats():
    rows = [job.image_row("a/1.jpg", JPG, "a", None, "train"), job.image_row("b/2.png", PNG, "b", None, None)]
    s = job.compute_stats(rows, job.IMAGE_CLASSIFICATION, None, 2)
    assert s == {"file_count": 2, "total_bytes": len(JPG) + len(PNG), "row_count": 2,
                 "classes": {"a": 1, "b": 1}, "splits": {"train": 1}, "duplicates_skipped": 2}
    t, _ = table("a,y\n1,x\n2,x\n3,z\n")
    assert job.compute_stats(t.rows, job.TABULAR_CLASSIFICATION, "y", 0)["classes"] == {"x": 2, "z": 1}


# ---- Hugging Face ----

FEATURES = [{"name": "image", "dtype": "image"}, {"name": "label", "dtype": {"class_label": {"names": {"0": "cat", "1": "dog"}}}}]
TABULAR = [{"name": "age", "dtype": "int64"}, {"name": "income", "dtype": "float64"}, {"name": "churn", "dtype": "bool"}]


def verify(task, cats, features, column=None):
    rep = Report()
    return job.verify_hf(task, cats, features, column, rep), rep


def test_hf_card_and_schema_agree():
    assert verify(job.IMAGE_CLASSIFICATION, ["image-classification"], FEATURES)[0] == "label"
    assert verify(job.TABULAR_REGRESSION, ["tabular-regression"], TABULAR, "income")[0] == "income"


def test_hf_card_disagrees_with_task():
    col, rep = verify(job.IMAGE_CLASSIFICATION, ["text-classification"], FEATURES)
    assert col is None and codes(rep) == [Code.HF_CARD_MISMATCH]


def test_hf_schema_disagrees_with_card():
    col, rep = verify(job.IMAGE_CLASSIFICATION, ["image-classification"], TABULAR)  # card says images, schema has none
    assert col is None and codes(rep) == [Code.HF_SCHEMA_MISMATCH]


def test_hf_ambiguous_or_invalid_column_choice():
    assert verify(job.TABULAR_CLASSIFICATION, ["tabular-classification"], TABULAR + [{"name": "tag", "dtype": "string"}])[1].failed
    col, rep = verify(job.TABULAR_CLASSIFICATION, ["tabular-classification"], TABULAR + [{"name": "tag", "dtype": "string"}], "tag")
    assert col == "tag" and not rep.failed
    assert verify(job.TABULAR_CLASSIFICATION, ["tabular-classification"], TABULAR, "income")[1].failed  # float cannot be a class


def test_hf_detection_schema():
    feats = [{"name": "image", "dtype": "image"}, {"name": "objects", "sequence": [{"name": "bbox"}, {"name": "category"}]}]
    assert verify(job.OBJECT_DETECTION, ["object-detection"], feats)[0] == "objects"


def test_hf_data_files_only_and_size_cap():
    rep = Report()
    files = job.select_hf_files([{"rfilename": "data/train-0.parquet", "size": 10}, {"rfilename": "loader.py", "size": 1},
                                 {"rfilename": "README.md", "size": 1}, {"rfilename": "a.csv", "size": 5}], LIMITS, rep)
    assert [f["rfilename"] for f in files] == ["data/train-0.parquet", "a.csv"] and not rep.failed
    rep = Report()
    job.select_hf_files([{"rfilename": "loader.py"}], LIMITS, rep)
    assert codes(rep) == [Code.HF_NO_DATA_FILES]
    rep = Report()
    job.select_hf_files([{"rfilename": "a.parquet", "size": 2_000}], LIMITS, rep)
    assert codes(rep) == [Code.HF_TOO_LARGE]


def test_hf_split_from_filename_and_class_names():
    assert [job.split_from_filename(p) for p in ("data/train-00000.parquet", "validation.csv", "val-1.csv", "test.jsonl", "all.csv")] == [
        "train", "validation", "validation", "test", None]
    assert job.class_names(FEATURES, "label") == ["cat", "dog"]


def test_hf_image_rows_map_class_ids_to_names():
    rep, objects = Report(), {}
    rows = job.hf_image_rows([{"image": {"bytes": JPG}, "label": 1}], job.IMAGE_CLASSIFICATION, "label", ["cat", "dog"], "train",
                             LIMITS, rep, fake_decode, objects.__setitem__)
    assert not rep.failed and rows[0]["label"] == "dog" and rows[0]["path"].startswith("dog/") and rows[0]["split"] == "train"
    rows = job.hf_image_rows([{"image": {"bytes": b"nope"}, "label": 0}], job.IMAGE_CLASSIFICATION, "label", ["cat"], None,
                             LIMITS, rep, fake_decode, objects.__setitem__)
    assert rows == [] and Code.CORRUPT_IMAGE in codes(rep)


# ---- orchestration with a fake store ----

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


def test_run_writes_a_failed_report_and_no_output_for_a_bad_zip():
    store = FakeStore(b"not a zip")
    rep = job.run(params(job.IMAGE_CLASSIFICATION), store, fake_decode)
    assert rep.failed and set(store.puts) == {job.REPORT_KEY}
    assert report_model.ImportReport.model_validate_json(store.puts[job.REPORT_KEY]).outcome == "failed"


def test_an_oversized_upload_is_refused_before_it_is_downloaded():
    store = FakeStore(b"x" * (LIMITS.upload_max_bytes + 1))
    store.download = lambda *_: pytest.fail("downloaded")
    rep = job.run(params(job.TABULAR_CLASSIFICATION, target="y"), store)
    assert codes(rep) == [Code.ARCHIVE_TOO_LARGE] and set(store.puts) == {job.REPORT_KEY}


def test_an_oversized_zip_member_is_refused_before_it_is_read_into_memory(monkeypatch):
    zf = make_zip({"a/1.jpg": b"\0" * (LIMITS.image_max_bytes + 1)})
    monkeypatch.setattr(job.ByteBudget, "read", lambda *a: pytest.fail("read into memory"))
    rep, objects = Report(), {}
    members = job.inspect_archive(zf.infolist(), 100, LIMITS, rep)
    job.ingest_classification(zf, members, LIMITS, rep, fake_decode, objects.__setitem__, None)
    assert Code.IMAGE_TOO_LARGE in codes(rep) and not objects


def test_run_internal_error_still_writes_report():
    store = FakeStore(b"")
    store.download = lambda *_: (_ for _ in ()).throw(RuntimeError("boom"))
    rep = job.run(params(job.TABULAR_CLASSIFICATION, target="y"), store)
    assert codes(rep) == [Code.INTERNAL_ERROR] and job.REPORT_KEY in store.puts


def test_run_stops_before_writing_when_the_archive_is_unsafe():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../x.jpg", JPG)
    store = FakeStore(buf.getvalue())
    rep = job.run(params(job.IMAGE_CLASSIFICATION), store, fake_decode)
    assert codes(rep) == [Code.ARCHIVE_UNSAFE_PATH] and set(store.puts) == {job.REPORT_KEY}


# ---- duplicates, ragged CSV, HF labels ----

def test_duplicate_archive_members_and_nfc_label_collisions_are_errors():
    zf, rep = make_zip({"cat/1.jpg": JPG}), Report()
    infos = zf.infolist() * 2
    assert len(job.inspect_archive(infos, 100, LIMITS, rep)) == 1 and codes(rep) == [Code.ARCHIVE_DUPLICATE_PATH]
    _, rep, objects = classify({"caf\u00e9/1.jpg": JPG, "cafe\u0301/1.jpg": PNG})  # precomposed vs combining accent
    assert Code.ARCHIVE_DUPLICATE_PATH in codes(rep) and len(objects) == 1


def test_coco_image_listed_twice_is_an_error():
    _, rep = detect(coco(images=[{"id": 1, "file_name": "img/1.jpg"}, {"id": 2, "file_name": "img/1.jpg"}]))
    assert Code.ARCHIVE_DUPLICATE_PATH in codes(rep)


def test_csv_row_longer_than_header_is_an_error_not_truncated():
    t, rep = table("a,y\n1,x\n2,z,extra\n")
    assert t is None and codes(rep) == [Code.CSV_ROW_TOO_LONG]


def test_hf_missing_label_is_an_error_not_the_string_none():
    rep, objects = Report(), {}
    for label in (None, 5):  # no label, class id out of range
        rows = job.hf_image_rows([{"image": {"bytes": JPG}, "label": label}], job.IMAGE_CLASSIFICATION, "label", ["cat"], None,
                                 LIMITS, rep, fake_decode, objects.__setitem__)
        assert rows == []
    assert codes(rep) == [Code.HF_SCHEMA_MISMATCH] * 2 and not objects


def test_hf_identical_records_collapse_to_one_row():
    rep, objects = Report(), {}
    rec = {"image": {"bytes": JPG}, "label": 0}
    rows = job.hf_image_rows([rec, rec], job.IMAGE_CLASSIFICATION, "label", ["cat"], None, LIMITS, rep, fake_decode, objects.__setitem__)
    assert len(rows) == 1 and codes(rep) == [Code.DUPLICATE_SKIPPED] and not rep.failed


def test_sniff_extension():
    assert [job.sniff_extension(d) for d in (JPG, PNG, b"GIF89a", b"BMxx", b"RIFF\0\0\0\0WEBPx", b"RIFF\0\0\0\0WAVE", b"")] == [
        ".jpg", ".png", ".gif", ".bmp", ".webp", None, None]


# ---- run() end to end with a fake store; parquet is swapped for JSON because pyarrow is not in this venv ----

@pytest.fixture
def fake_parquet(monkeypatch):
    monkeypatch.setattr(job, "index_parquet", lambda rows: json.dumps(rows).encode())
    monkeypatch.setattr(job, "table_parquet", lambda rows, table: json.dumps(rows).encode())
    monkeypatch.setattr(job, "read_parquet", lambda data, max_bytes=None: json.loads(data))


def zip_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def final_report(store):
    return report_model.ImportReport.model_validate_json(store.puts[job.REPORT_KEY])


def test_run_classification_success_writes_objects_index_and_ok_report(fake_parquet):
    store = FakeStore(zip_bytes({"cat/1.jpg": JPG, "dog/2.png": PNG}))
    rep = job.run(params(job.IMAGE_CLASSIFICATION, split="train"), store, fake_decode)
    assert not rep.failed
    assert set(store.puts) == {"out/objects/cat/1.jpg", "out/objects/dog/2.png", "out/index.parquet", job.REPORT_KEY}
    index = json.loads(store.puts["out/index.parquet"])
    assert [(r["path"], r["label"], r["split"]) for r in index] == [("cat/1.jpg", "cat", "train"), ("dog/2.png", "dog", "train")]
    final = final_report(store)
    assert final.outcome == "ok" and final.stats.row_count == 2 and final.stats.classes == {"cat": 1, "dog": 1}


def test_run_merges_with_the_base_index_and_skips_duplicates(fake_parquet, monkeypatch):
    base = [job.image_row("cat/1.jpg", JPG, "cat", None, None), job.image_row("bird/9.jpg", JPG, "bird", None, None)]
    monkeypatch.setattr(job, "http_get", lambda url: json.dumps(base).encode())
    store = FakeStore(zip_bytes({"cat/1.jpg": JPG, "dog/2.png": PNG}))
    rep = job.run(params(job.IMAGE_CLASSIFICATION, base={"index_url": "http://base/index"}), store, fake_decode)
    assert not rep.failed
    assert [r["path"] for r in json.loads(store.puts["out/index.parquet"])] == ["cat/1.jpg", "bird/9.jpg", "dog/2.png"]
    # additive staging: out/ carries the merged index and the new objects only; the base's objects stay in lakeFS
    assert "out/objects/bird/9.jpg" not in store.puts and "out/objects/dog/2.png" in store.puts
    assert final_report(store).stats.duplicates_skipped == 1


def test_run_base_conflict_writes_no_index(fake_parquet, monkeypatch):
    monkeypatch.setattr(job, "http_get", lambda url: json.dumps([job.image_row("cat/1.jpg", PNG, "cat", None, None)]).encode())
    store = FakeStore(zip_bytes({"cat/1.jpg": JPG}))
    rep = job.run(params(job.IMAGE_CLASSIFICATION, base={"index_url": "http://base/index"}), store, fake_decode)
    assert codes(rep) == [Code.MERGE_CONFLICT] and "out/index.parquet" not in store.puts
    assert final_report(store).outcome == "failed"


def test_run_csv_success_and_merge_with_a_base(fake_parquet, monkeypatch):
    store = FakeStore(b"a,b,y\n1,,x\n2,,z\n")
    rep = job.run(params(job.TABULAR_CLASSIFICATION, target="y", split="test"), store)
    assert not rep.failed and set(store.puts) == {"out/data.parquet", "out/schema.json", job.REPORT_KEY}
    schema = json.loads(store.puts["out/schema.json"])
    assert schema == {"columns": [{"name": "a", "type": "int64"}, {"name": "b", "type": "string"}, {"name": "y", "type": "string"}]}
    assert json.loads(store.puts["out/data.parquet"])[0] == {"a": 1, "b": None, "y": "x", "split": "test"}
    assert final_report(store).stats.classes == {"x": 1, "z": 1}

    base_rows = [{"a": 0, "b": None, "y": "q", "split": None}]
    monkeypatch.setattr(job, "http_get", lambda url: json.dumps(schema if url.endswith("schema") else base_rows).encode())
    store = FakeStore(b"a,b,y\n1,,x\n2,,z\n")
    rep = job.run(params(job.TABULAR_CLASSIFICATION, target="y", base={"data_url": "http://b/data", "schema_url": "http://b/schema"}), store)
    assert not rep.failed and len(json.loads(store.puts["out/data.parquet"])) == 3
    store = FakeStore(b"a,b,y\n1.5,,x\n2,,z\n")  # a is now float64: schema differs from the base
    rep = job.run(params(job.TABULAR_CLASSIFICATION, target="y", base={"data_url": "http://b/data", "schema_url": "http://b/schema"}), store)
    assert codes(rep) == [Code.SCHEMA_MISMATCH] and "out/data.parquet" not in store.puts


HF_INFO = {"sha": "c0ffee", "cardData": {"license": "mit", "task_categories": ["tabular-classification"],
                                         "dataset_info": {"features": [{"name": "age", "dtype": "int64"},
                                                                       {"name": "churn", "dtype": "bool"}]}},
           "siblings": [{"rfilename": "train.jsonl", "size": 10}, {"rfilename": "loader.py", "size": 1}]}


def test_run_hf_tabular_success_pins_revision_and_keeps_split(fake_parquet, monkeypatch):
    fetched = []
    monkeypatch.setattr(job, "hf_json", lambda repo, revision: HF_INFO)

    def fake_get(url, max_bytes=None):
        fetched.append(url)
        return b'{"age": 30, "churn": true}\n{"age": 41, "churn": false}\n'

    monkeypatch.setattr(job, "http_get", fake_get)
    store = FakeStore()
    rep = job.run(params(job.TABULAR_CLASSIFICATION, source="huggingface", hf={"repo": "o/r", "revision": "c0ffee"}), store)
    assert not rep.failed and fetched == ["https://huggingface.co/datasets/o/r/resolve/c0ffee/train.jsonl"]
    assert [r["split"] for r in json.loads(store.puts["out/data.parquet"])] == ["train", "train"]
    final = final_report(store)
    assert (final.revision, final.license, final.stats.classes) == ("c0ffee", "mit", {"True": 1, "False": 1})


def test_run_hf_image_success_and_card_mismatch(fake_parquet, monkeypatch):
    info = {"sha": "abc", "cardData": {"task_categories": ["image-classification"], "dataset_info": {"features": FEATURES}},
            "siblings": [{"rfilename": "test-0.parquet", "size": 5}]}
    monkeypatch.setattr(job, "hf_json", lambda repo, revision: info)
    monkeypatch.setattr(job, "http_get", lambda url, max_bytes=None: b"")
    monkeypatch.setattr(job, "read_parquet", lambda data, max_bytes=None: [{"image": {"bytes": JPG}, "label": 1}])
    store = FakeStore()
    rep = job.run(params(job.IMAGE_CLASSIFICATION, source="huggingface", hf={"repo": "o/r", "revision": "abc"}), store, fake_decode)
    assert not rep.failed
    (row,) = json.loads(store.puts["out/index.parquet"])
    assert row["label"] == "dog" and row["split"] == "test" and f"out/objects/{row['path']}" in store.puts
    store = FakeStore()
    rep = job.run(params(job.TABULAR_CLASSIFICATION, source="huggingface", target="x", hf={"repo": "o/r", "revision": "abc"}), store)
    assert Code.HF_CARD_MISMATCH in codes(rep) and not any(k.startswith("out/") for k in store.puts)


def test_run_reports_a_timeout():
    store = FakeStore(zip_bytes({"cat/1.jpg": JPG}))

    def slow(_data):
        raise TimeoutError

    rep = job.run(params(job.IMAGE_CLASSIFICATION), store, slow)
    assert codes(rep) == [Code.JOB_TIMEOUT] and final_report(store).outcome == "failed"


def test_run_with_bad_limits_still_writes_a_report():
    store = FakeStore(b"")
    rep = job.run({"source": "upload", "task": job.TABULAR_CLASSIFICATION, "limits": {}}, store)
    assert codes(rep) == [Code.INTERNAL_ERROR] and final_report(store).outcome == "failed"


def test_main_arms_the_alarm_and_maps_failure_to_exit_code(monkeypatch):
    alarms, handlers = [], {}
    monkeypatch.setattr(job.signal, "alarm", alarms.append)
    monkeypatch.setattr(job.signal, "signal", lambda sig, fn: handlers.update({sig: fn}))
    for var in ("STAGING_ENDPOINT", "STAGING_ACCESS_KEY", "STAGING_SECRET_KEY"):
        monkeypatch.setenv(var, "x")
    monkeypatch.setattr(job, "S3Store", lambda *a: FakeStore())
    for failed, code in ((True, 1), (False, 0)):
        rep = Report()
        if failed:
            rep.error(Code.NO_DATA, "x")
        monkeypatch.setattr(job, "run", lambda params, store, rep=rep: rep)
        argv = ["ingest.py", json.dumps({"bucket": "b", "prefix": "p/", **params(job.IMAGE_CLASSIFICATION)})]
        assert job.main(argv) == code
    assert alarms == [LIMITS.job_timeout_seconds, job.NO_ALARM] * 2  # armed, then cancelled after run()
    with pytest.raises(TimeoutError):
        handlers[job.signal.SIGALRM]()


# ---- named keys, real parquet, HF hardening, timeout alarm ----

def test_key_enums_mirror_the_layout_and_the_row_builders_use_them():
    assert job.BOX_FIELDS == layout.BOX_FIELDS and job.INDEX_COLUMNS == layout.INDEX_COLUMNS
    assert tuple(job.image_row("a/1.jpg", JPG, "a", [], None)) == layout.INDEX_COLUMNS
    box = job.parse_coco({"images": [{"id": 1, "file_name": "x.jpg"}], "categories": [{"id": 1, "name": "c"}],
                          "annotations": [{"image_id": 1, "category_id": 1, "bbox": [1, 2, 3, 4]}]}, Report())[0]["boxes"][0]
    assert tuple(box) == layout.BOX_FIELDS


def test_job_params_keys_cover_what_core_api_sends():
    from app.integrations.data_plane import ingest as core_ingest  # builds the params dict from literals

    src = Path(core_ingest.__file__).read_text()
    sent = set(re.findall(r'"(\w+)":', src.split("job = {")[1].split("creds =")[0]))
    assert sent <= {p.value for p in job.Param}


@pytest.mark.parametrize("name", ["a/b", "..", ".", "", "a\\b"])
def test_hf_label_with_path_characters_is_unusable(name):
    assert job.hf_label(0, [name]) is None and job.hf_label(name, None) is None


def test_hf_file_without_a_size_cannot_dodge_the_repo_cap():
    rep = Report()
    job.select_hf_files([{"rfilename": "a.parquet"}], LIMITS, rep)
    assert codes(rep) == [Code.HF_TOO_LARGE]


def test_http_get_stops_reading_at_the_cap(monkeypatch):
    class Resp(io.BytesIO):
        def __enter__(self):
            return self

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout: Resp(b"x" * 50))
    assert job.http_get("u", 50) == b"x" * 50
    with pytest.raises(job.LimitExceeded):
        job.http_get("u", 49)


def test_byte_budget_take_raises_past_the_limit():
    budget = job.ByteBudget(10)
    budget.take(10)
    with pytest.raises(job.LimitExceeded):
        budget.take(1)


def test_run_cancels_the_alarm_before_writing_the_report(monkeypatch):
    calls = []
    monkeypatch.setattr(job.signal, "alarm", lambda n: calls.append(("alarm", n)))
    store = FakeStore(b"not a zip")
    store.put = lambda key, data: calls.append(("put", key))
    job.run(params(job.IMAGE_CLASSIFICATION), store, fake_decode)
    assert calls == [("alarm", job.NO_ALARM), ("put", job.REPORT_KEY)]


# the real pyarrow paths (pyarrow is a core-api dependency for previews); PIL is not, so decoding stays injected

def test_real_parquet_round_trip_of_an_image_index():
    pytest.importorskip("pyarrow")
    boxes = [{"category": "c", "x": 1.0, "y": 2.0, "width": 3.0, "height": 4.0}]
    rows = [job.image_row("a/1.jpg", JPG, "a", None, "train"), job.image_row("2.png", PNG, None, boxes, None)]
    assert job.read_parquet(job.index_parquet(rows)) == rows
    assert job.read_parquet(job.index_parquet([])) == []


def test_real_parquet_round_trip_of_a_table_keeps_types_and_all_empty_columns():
    pytest.importorskip("pyarrow")
    import pyarrow.parquet as pq

    t, _ = table("n,f,b,e,y\n1,1.5,true,,x\n2,,false,,z\n")
    data = job.table_parquet(t.rows, t)
    assert job.read_parquet(data) == t.rows
    assert [(f.name, str(f.type)) for f in pq.read_schema(io.BytesIO(data))] == [
        ("n", "int64"), ("f", "double"), ("b", "bool"), ("e", "string"), ("y", "string"), ("split", "string")]


def test_read_parquet_refuses_a_file_that_inflates_past_the_budget():
    pytest.importorskip("pyarrow")
    data = job.table_parquet(*(lambda t: (t.rows, t))(table("a,y\n" + "1,x\n" * 50)[0]))
    assert len(job.read_parquet(data, 10**9)) == 50
    with pytest.raises(job.LimitExceeded):
        job.read_parquet(data, 1)
