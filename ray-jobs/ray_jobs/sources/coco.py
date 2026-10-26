"""COCO object detection in a zip: annotations.json plus the images it lists."""
import json
import posixpath
from typing import Any

from ray_jobs.common.constants import COCO_FILE
from ray_jobs.common.enums import BOX_FIELDS, Box, Code, Coco, Col
from ray_jobs.common.labels import check_label_collisions, normalise_label
from ray_jobs.common.report import Report
from ray_jobs.common.rows import image_row
from ray_jobs.common.store import Sink
from ray_jobs.sources.archive import ByteBudget, is_unsafe_path
from ray_jobs.sources.images import check_image, has_image_extension, image_too_large


def parse_coco(doc: Any, report: Report) -> list[dict]:
    """Validate a COCO document; returns [{file_name, boxes:[{category,x,y,width,height}]}] (keys: Coco.FILE_NAME, Col.BOXES, Box)."""
    def bad(msg):
        report.error(Code.COCO_INVALID, msg, COCO_FILE)
        return []

    if not isinstance(doc, dict) or not all(isinstance(doc.get(k), list) for k in (Coco.IMAGES, Coco.ANNOTATIONS, Coco.CATEGORIES)):
        return bad("expected an object with images, annotations and categories lists")
    names: dict[Any, str] = {}
    for c in doc[Coco.CATEGORIES]:
        if not isinstance(c, dict) or Coco.ID not in c or not isinstance(c.get(Coco.NAME), str):
            return bad("category needs an id and a name")
        if c[Coco.ID] in names:
            return bad(f"duplicate category id {c[Coco.ID]}")
        names[c[Coco.ID]] = normalise_label(c[Coco.NAME])
    if len(set(names.values())) != len(names):
        return bad("two category ids share one name")
    images: dict[Any, dict] = {}
    for im in doc[Coco.IMAGES]:
        if not isinstance(im, dict) or Coco.ID not in im or not isinstance(im.get(Coco.FILE_NAME), str):
            return bad("image needs an id and a file_name")
        if im[Coco.ID] in images:
            return bad(f"duplicate image id {im[Coco.ID]}")
        images[im[Coco.ID]] = {Coco.FILE_NAME: im[Coco.FILE_NAME], Col.BOXES: []}
    seen_ann = set()
    for a in doc[Coco.ANNOTATIONS]:
        if not isinstance(a, dict) or a.get(Coco.IMAGE_ID) not in images or a.get(Coco.CATEGORY_ID) not in names:
            return bad("annotation references an unknown image or category")
        if Coco.ID in a:
            if a[Coco.ID] in seen_ann:
                return bad(f"duplicate annotation id {a[Coco.ID]}")
            seen_ann.add(a[Coco.ID])
        box = a.get(Coco.BBOX)
        if not (isinstance(box, list) and len(box) == 4 and all(isinstance(v, (int, float)) for v in box)) or box[2] <= 0 or box[3] <= 0:
            return bad("bbox must be [x, y, width, height] with positive size")
        images[a[Coco.IMAGE_ID]][Col.BOXES].append(dict(zip(BOX_FIELDS, (names[a[Coco.CATEGORY_ID]], *box))))
    return list(images.values())


def ingest_detection(zf, members, limits, report, decode, sink: Sink, split: str | None) -> list[dict]:
    coco = [m for m in members if posixpath.basename(m.filename) == COCO_FILE]
    if len(coco) != 1:
        report.error(Code.COCO_INVALID, f"expected exactly one {COCO_FILE}, found {len(coco)}")
        return []
    budget = ByteBudget(limits.extracted_max_bytes)
    try:
        doc = json.loads(budget.read(zf, coco[0]))
    except ValueError:
        report.error(Code.COCO_INVALID, "not valid JSON", coco[0].filename)
        return []
    entries = parse_coco(doc, report)
    by_name = {m.filename: m for m in members}
    base = posixpath.dirname(coco[0].filename)
    rows, seen_paths = [], set()
    for entry in entries:
        path = posixpath.normpath(posixpath.join(base, entry[Coco.FILE_NAME]))
        info = by_name.get(path)
        if info is None or is_unsafe_path(path):
            report.error(Code.COCO_INVALID, "image listed in annotations is not in the archive", entry[Coco.FILE_NAME])
            continue
        if not has_image_extension(path):
            report.error(Code.UNSUPPORTED_EXTENSION, "not an image extension", path)
            continue
        if path in seen_paths:
            report.error(Code.ARCHIVE_DUPLICATE_PATH, "image listed twice in the annotations", path)
            continue
        seen_paths.add(path)
        if image_too_large(info, path, limits, report):
            continue
        data = budget.read(zf, info)
        if check_image(data, path, limits, report, decode):
            sink(path, data)
            rows.append(image_row(path, data, None, entry[Col.BOXES], split))
    check_label_collisions({b[Box.CATEGORY] for r in rows for b in r[Col.BOXES]}, report)
    if not rows and not report.failed:
        report.error(Code.NO_DATA, "no annotated images found")
    return rows
