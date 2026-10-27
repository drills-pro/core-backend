import pytest

from ray_training.constants import Col, Split
from ray_training.data import hash_split, load_dataset, split_for_training
from ray_training.errors import BadData
from ray_training.spec import parse_spec
from tests.support import MemStore, classification_rows, detection_rows, fake_get, spec_text  # noqa: F401


def spec_for(rows, task="image-classification"):
    return parse_spec(spec_text(task, "m", rows))


def test_hash_split_is_deterministic_and_roughly_80_10_10():
    shas = [f"{i:08x}" + "0" * 56 for i in range(0, 2**32, 2**32 // 1000)]
    first = [hash_split(s) for s in shas]
    assert first == [hash_split(s) for s in shas]
    share = {s: first.count(s) / len(first) for s in Split}
    assert 0.75 < share[Split.TRAIN] < 0.85 and 0.05 < share[Split.VALIDATION] < 0.15 and 0.05 < share[Split.TEST] < 0.15


def test_the_split_column_wins_and_null_rows_are_hashed(tmp_path):
    rows, files = classification_rows()
    rows[0][Col.SPLIT] = None
    ds = load_dataset(spec_for(rows), tmp_path, fake_get(rows, files))
    assert ds.samples[0].split == hash_split(rows[0][Col.SHA256])
    assert [s.split for s in ds.samples[1:]] == [Split(r[Col.SPLIT]) for r in rows[1:]]
    assert ds.classes == ["blue", "red"]


def test_detection_boxes_are_indexed_by_sorted_category(tmp_path):
    rows, files = detection_rows()
    ds = load_dataset(spec_for(rows, "object-detection"), tmp_path, fake_get(rows, files))
    assert ds.classes == ["cat", "dog"] and [b[0] for b in ds.samples[0].boxes] == [1, 0]


def test_an_image_missing_from_the_manifest_is_bad_data(tmp_path):
    rows, files = classification_rows()
    with pytest.raises(BadData, match="no URL"):
        load_dataset(spec_for(rows), tmp_path, fake_get(rows, files, manifest_rows=rows[:-1]))


def test_training_needs_both_sides(tmp_path):
    rows, files = classification_rows()
    for r in rows:
        r[Col.SPLIT] = "train"
    with pytest.raises(BadData):
        split_for_training(load_dataset(spec_for(rows), tmp_path, fake_get(rows, files)))
