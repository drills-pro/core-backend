import pytest

from ray_jobs.common.enums import Code
from ray_jobs.common.report import Report
from ray_jobs.sources.archive import ByteBudget, inspect_archive
from ray_jobs.sources.images import ingest_classification
from tests.support import JPG, LIMITS, PNG, codes, fake_decode, make_zip


def classify(files, **kw):
    zf, rep, objects = make_zip(files, **kw), Report(), {}
    members = inspect_archive(zf.infolist(), sum(len(v) for v in files.values()) or 1, LIMITS, rep)
    rows = ingest_classification(zf, members, LIMITS, rep, fake_decode, objects.__setitem__, None)
    return rows, rep, objects


def test_valid_classification_zip():
    rows, rep, objects = classify({"cat/1.jpg": JPG, "dog/2.png": PNG, "__MACOSX/cat/._1.jpg": b"junk"})
    assert not rep.failed
    assert [(r["path"], r["label"], r["size"]) for r in rows] == [("cat/1.jpg", "cat", len(JPG)), ("dog/2.png", "dog", len(PNG))]
    assert set(objects) == {"cat/1.jpg", "dog/2.png"}


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


def test_nfc_label_collisions_are_errors():
    _, rep, objects = classify({"caf\u00e9/1.jpg": JPG, "cafe\u0301/1.jpg": PNG})  # precomposed vs combining accent
    assert Code.ARCHIVE_DUPLICATE_PATH in codes(rep) and len(objects) == 1


def test_an_oversized_zip_member_is_refused_before_it_is_read_into_memory(monkeypatch):
    zf = make_zip({"a/1.jpg": b"\0" * (LIMITS.image_max_bytes + 1)})
    monkeypatch.setattr(ByteBudget, "read", lambda *a: pytest.fail("read into memory"))
    rep, objects = Report(), {}
    members = inspect_archive(zf.infolist(), 100, LIMITS, rep)
    ingest_classification(zf, members, LIMITS, rep, fake_decode, objects.__setitem__, None)
    assert Code.IMAGE_TOO_LARGE in codes(rep) and not objects


def test_a_wrapper_folder_around_the_classes_is_not_a_class():
    rows, rep, _ = classify({"ds/cat/1.jpg": JPG, "ds/dog/2.png": PNG})
    assert not rep.failed and [(r["path"], r["label"]) for r in rows] == [("cat/1.jpg", "cat"), ("dog/2.png", "dog")]


def test_split_folders_set_the_split_and_keep_files_of_one_name_apart():
    rows, rep, objects = classify({"train/cat/1.jpg": JPG, "val/cat/1.jpg": PNG, "Test/dog/2.jpg": JPG})
    assert not rep.failed
    assert [(r["path"], r["label"], r["split"]) for r in rows] == [
        ("train/cat/1.jpg", "cat", "train"), ("val/cat/1.jpg", "cat", "validation"), ("Test/dog/2.jpg", "dog", "test")]
    assert len(objects) == 3


def test_a_wrapper_around_split_folders_and_a_class_named_like_a_split():
    rows, _, _ = classify({"ds/train/cat/1.jpg": JPG, "ds/test/cat/2.jpg": PNG})
    assert [(r["path"], r["split"]) for r in rows] == [("train/cat/1.jpg", "train"), ("test/cat/2.jpg", "test")]
    rows, _, _ = classify({"train/1.jpg": JPG, "test/2.jpg": PNG})  # two classes that happen to be called train and test
    assert [(r["label"], r["split"]) for r in rows] == [("train", None), ("test", None)]


def test_a_folder_of_one_name_that_is_not_every_images_first_folder_is_left_alone():
    rows, _, _ = classify({"cat/sub/1.jpg": JPG, "dog/sub/2.jpg": PNG})
    assert [r["label"] for r in rows] == ["cat", "dog"]


def test_macos_resource_forks_outside___macosx_are_skipped_not_corrupt():
    rows, rep, _ = classify({"cat/1.jpg": JPG, "cat/._1.jpg": b"BAD" + JPG, "dog/2.png": PNG})
    assert not rep.failed and [r["path"] for r in rows] == ["cat/1.jpg", "dog/2.png"]
