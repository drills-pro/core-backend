import numpy as np
import pytest

from ray_core.constants import Col, Split
from ray_core.data import carve_held_out, hash_split, load_image_dataset, require_classes, split_for_training
from ray_core.errors import BadData
from ray_core.spec import parse_spec
from tests.support import MemStore, classification_rows, detection_rows, fake_get, spec_text  # noqa: F401


def spec_for(rows, task="image-classification"):
    return parse_spec(spec_text(task, "m", rows))


def load_dataset(spec, work, get):
    return load_image_dataset(spec, work, get, detection=spec.task == "object-detection")


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


def test_training_needs_both_sides_and_a_fifth_is_held_out_when_none_is(tmp_path):
    rows, files = classification_rows()
    for r in rows:
        r[Col.SPLIT] = "train"
    ds = load_dataset(spec_for(rows), tmp_path, fake_get(rows, files))
    train, held_out = split_for_training(ds)  # nothing held out: a strided fifth is
    assert len(train) + len(held_out) == len(rows) and len(held_out) == len(rows[::5])
    with pytest.raises(BadData):  # one image cannot be both
        split_for_training(load_dataset(spec_for(rows[:1]), tmp_path / "one", fake_get(rows[:1], files)))


def test_carve_held_out_is_strided_so_every_class_is_reached_and_leaves_a_given_held_out_alone():
    items = ["a1", "a2", "a3", "a4", "a5", "a6", "b1", "b2", "b3", "b4"]  # grouped by class, as an index is
    train, held_out = carve_held_out(items, [])
    assert held_out == ["a1", "a6"] and "a1" not in train and len(train) == 8
    assert carve_held_out(items, ["x"]) == (items, ["x"])
    assert carve_held_out(["a", "b"], []) == (["b"], ["a"]) and carve_held_out(["a"], []) == (["a"], [])
    equal = [("same",)] * 6  # equal items are told apart by position
    assert [len(part) for part in carve_held_out(equal, [])] == [4, 2]


def test_a_classifier_needs_two_classes(tmp_path):
    rows, files = classification_rows()
    for r in rows:
        r[Col.LABEL] = "red"
    with pytest.raises(BadData, match="at least 2 classes"):
        load_dataset(spec_for(rows), tmp_path, fake_get(rows, files))
    require_classes(["a", "b"])


def test_load_image_applies_the_specs_normalisation(tmp_path):
    from ray_core.result import load_image
    from tests.support import SIZE, png

    file = tmp_path / "gray.png"
    file.write_bytes(png((51, 51, 51)))  # 51 / 255 = 0.2
    pre = spec_for(classification_rows()[0]).preprocessing
    tensor, w, h = load_image(file, 8, pre)
    assert (w, h, tuple(tensor.shape)) == (SIZE, SIZE, (3, 8, 8))
    assert np.allclose(tensor.numpy(), (0.2 - 0.5) / 0.25, atol=1e-5)  # PREPROCESSING: mean 0.5, std 0.25
