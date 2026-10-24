import json

import pytest

from ray_jobs.common.enums import BOX_FIELDS, Code
from ray_jobs.common.report import Report
from ray_jobs.sources.coco import ingest_detection, parse_coco
from tests.support import JPG, LIMITS, PNG, codes, fake_decode, make_zip


def coco(**over):
    doc = {"images": [{"id": 1, "file_name": "img/1.jpg"}, {"id": 2, "file_name": "img/2.png"}],
           "categories": [{"id": 1, "name": "cat"}, {"id": 2, "name": "dog"}],
           "annotations": [{"id": 1, "image_id": 1, "category_id": 2, "bbox": [1, 2, 3, 4]}]}
    return {**doc, **over}


def detect(doc, files=None):
    files = {"data/annotations.json": json.dumps(doc).encode(), "data/img/1.jpg": JPG, "data/img/2.png": PNG, **(files or {})}
    zf, rep = make_zip(files), Report()
    rows = ingest_detection(zf, zf.infolist(), LIMITS, rep, fake_decode, lambda *_: None, "train")
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
    ingest_detection(zf, zf.infolist(), LIMITS, rep, fake_decode, lambda *_: None, None)
    assert Code.COCO_INVALID in codes(rep)


def test_coco_image_listed_twice_is_an_error():
    _, rep = detect(coco(images=[{"id": 1, "file_name": "img/1.jpg"}, {"id": 2, "file_name": "img/1.jpg"}]))
    assert Code.ARCHIVE_DUPLICATE_PATH in codes(rep)


def test_parsed_boxes_use_the_layout_field_names():
    box = parse_coco({"images": [{"id": 1, "file_name": "x.jpg"}], "categories": [{"id": 1, "name": "c"}],
                      "annotations": [{"image_id": 1, "category_id": 1, "bbox": [1, 2, 3, 4]}]}, Report())[0]["boxes"][0]
    assert tuple(box) == BOX_FIELDS
