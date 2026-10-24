import pytest

from ray_jobs.common.constants import (
    IMAGE_CLASSIFICATION, OBJECT_DETECTION, TABULAR_CLASSIFICATION, TABULAR_REGRESSION)
from ray_jobs.common.enums import Code
from ray_jobs.common.report import Report
from ray_jobs.sources.huggingface import (
    class_names, hf_image_rows, hf_label, select_hf_files, sniff_extension, split_from_filename, verify_hf)
from tests.support import JPG, LIMITS, PNG, codes, fake_decode

FEATURES = [{"name": "image", "dtype": "image"}, {"name": "label", "dtype": {"class_label": {"names": {"0": "cat", "1": "dog"}}}}]
TABULAR = [{"name": "age", "dtype": "int64"}, {"name": "income", "dtype": "float64"}, {"name": "churn", "dtype": "bool"}]


def verify(task, cats, features, column=None):
    rep = Report()
    return verify_hf(task, cats, features, column, rep), rep


def test_hf_card_and_schema_agree():
    assert verify(IMAGE_CLASSIFICATION, ["image-classification"], FEATURES)[0] == "label"
    assert verify(TABULAR_REGRESSION, ["tabular-regression"], TABULAR, "income")[0] == "income"


def test_hf_card_disagrees_with_task():
    col, rep = verify(IMAGE_CLASSIFICATION, ["text-classification"], FEATURES)
    assert col is None and codes(rep) == [Code.HF_CARD_MISMATCH]


def test_hf_schema_disagrees_with_card():
    col, rep = verify(IMAGE_CLASSIFICATION, ["image-classification"], TABULAR)  # card says images, schema has none
    assert col is None and codes(rep) == [Code.HF_SCHEMA_MISMATCH]


def test_hf_ambiguous_or_invalid_column_choice():
    assert verify(TABULAR_CLASSIFICATION, ["tabular-classification"], TABULAR + [{"name": "tag", "dtype": "string"}])[1].failed
    col, rep = verify(TABULAR_CLASSIFICATION, ["tabular-classification"], TABULAR + [{"name": "tag", "dtype": "string"}], "tag")
    assert col == "tag" and not rep.failed
    assert verify(TABULAR_CLASSIFICATION, ["tabular-classification"], TABULAR, "income")[1].failed  # float cannot be a class


def test_hf_detection_schema():
    feats = [{"name": "image", "dtype": "image"}, {"name": "objects", "sequence": [{"name": "bbox"}, {"name": "category"}]}]
    assert verify(OBJECT_DETECTION, ["object-detection"], feats)[0] == "objects"


def test_hf_data_files_only_and_size_cap():
    rep = Report()
    files = select_hf_files([{"rfilename": "data/train-0.parquet", "size": 10}, {"rfilename": "loader.py", "size": 1},
                             {"rfilename": "README.md", "size": 1}, {"rfilename": "a.csv", "size": 5}], LIMITS, rep)
    assert [f["rfilename"] for f in files] == ["data/train-0.parquet", "a.csv"] and not rep.failed
    rep = Report()
    select_hf_files([{"rfilename": "loader.py"}], LIMITS, rep)
    assert codes(rep) == [Code.HF_NO_DATA_FILES]
    rep = Report()
    select_hf_files([{"rfilename": "a.parquet", "size": 2_000}], LIMITS, rep)
    assert codes(rep) == [Code.HF_TOO_LARGE]


def test_hf_file_without_a_size_cannot_dodge_the_repo_cap():
    rep = Report()
    select_hf_files([{"rfilename": "a.parquet"}], LIMITS, rep)
    assert codes(rep) == [Code.HF_TOO_LARGE]


def test_hf_split_from_filename_and_class_names():
    assert [split_from_filename(p) for p in ("data/train-00000.parquet", "validation.csv", "val-1.csv", "test.jsonl", "all.csv")] == [
        "train", "validation", "validation", "test", None]
    assert class_names(FEATURES, "label") == ["cat", "dog"]


def test_hf_image_rows_map_class_ids_to_names():
    rep, objects = Report(), {}
    rows = hf_image_rows([{"image": {"bytes": JPG}, "label": 1}], IMAGE_CLASSIFICATION, "label", ["cat", "dog"], "train",
                         LIMITS, rep, fake_decode, objects.__setitem__)
    assert not rep.failed and rows[0]["label"] == "dog" and rows[0]["path"].startswith("dog/") and rows[0]["split"] == "train"
    rows = hf_image_rows([{"image": {"bytes": b"nope"}, "label": 0}], IMAGE_CLASSIFICATION, "label", ["cat"], None,
                         LIMITS, rep, fake_decode, objects.__setitem__)
    assert rows == [] and Code.CORRUPT_IMAGE in codes(rep)


def test_hf_missing_label_is_an_error_not_the_string_none():
    rep, objects = Report(), {}
    for label in (None, 5):  # no label, class id out of range
        rows = hf_image_rows([{"image": {"bytes": JPG}, "label": label}], IMAGE_CLASSIFICATION, "label", ["cat"], None,
                             LIMITS, rep, fake_decode, objects.__setitem__)
        assert rows == []
    assert codes(rep) == [Code.HF_SCHEMA_MISMATCH] * 2 and not objects


def test_hf_identical_records_collapse_to_one_row():
    rep, objects = Report(), {}
    rec = {"image": {"bytes": JPG}, "label": 0}
    rows = hf_image_rows([rec, rec], IMAGE_CLASSIFICATION, "label", ["cat"], None, LIMITS, rep, fake_decode, objects.__setitem__)
    assert len(rows) == 1 and codes(rep) == [Code.DUPLICATE_SKIPPED] and not rep.failed


def test_sniff_extension():
    assert [sniff_extension(d) for d in (JPG, PNG, b"GIF89a", b"BMxx", b"RIFF\0\0\0\0WEBPx", b"RIFF\0\0\0\0WAVE", b"")] == [
        ".jpg", ".png", ".gif", ".bmp", ".webp", None, None]


@pytest.mark.parametrize("name", ["a/b", "..", ".", "", "a\\b"])
def test_hf_label_with_path_characters_is_unusable(name):
    assert hf_label(0, [name]) is None and hf_label(name, None) is None
