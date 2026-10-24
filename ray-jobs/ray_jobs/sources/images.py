"""Image sources: a zip of `<class>/<image>` folders, and the checks every image goes through."""
import io
import posixpath
import zipfile
from typing import Callable

from ray_jobs.common.constants import IMAGE_EXTENSIONS
from ray_jobs.common.enums import Code, Col
from ray_jobs.common.labels import check_label_collisions, normalise_label
from ray_jobs.common.limits import Limits
from ray_jobs.common.report import Report
from ray_jobs.common.rows import image_row
from ray_jobs.common.store import Sink
from ray_jobs.sources.archive import ByteBudget


def image_too_large(info: zipfile.ZipInfo, path: str, limits: Limits, report: Report) -> bool:
    """Judge an image member by its declared size before reading it: zipfile never yields more than that, so a
    huge member is refused without being held in memory."""
    if info.file_size <= limits.image_max_bytes:
        return False
    report.error(Code.IMAGE_TOO_LARGE, f"{info.file_size} bytes, limit {limits.image_max_bytes}", path)
    return True


def pil_decode(data: bytes) -> tuple[int, int]:
    from PIL import Image  # lazy: in the Ray image, not in core-api's venv

    with Image.open(io.BytesIO(data)) as im:
        im.verify()
    with Image.open(io.BytesIO(data)) as im:
        return im.size


def check_image(data: bytes, path: str, limits: Limits, report: Report, decode: Callable[[bytes], tuple[int, int]]) -> bool:
    if len(data) > limits.image_max_bytes:
        report.error(Code.IMAGE_TOO_LARGE, f"{len(data)} bytes, limit {limits.image_max_bytes}", path)
        return False
    try:
        width, height = decode(data)  # a real header decode, the extension alone proves nothing
    except TimeoutError:  # the SIGALRM job timeout must not be read as a corrupt image
        raise
    except Exception:
        report.error(Code.CORRUPT_IMAGE, "not a decodable image", path)
        return False
    if max(width, height) > limits.image_max_dimension:
        report.error(Code.IMAGE_TOO_LARGE, f"{width}x{height}, limit {limits.image_max_dimension}", path)
        return False
    return True


def has_image_extension(name: str) -> bool:
    return posixpath.splitext(name)[1].lower() in IMAGE_EXTENSIONS


def ingest_classification(zf, members, limits, report, decode, sink: Sink, split: str | None) -> list[dict]:
    """`<class>/<image>`: the first directory is the label."""
    budget = ByteBudget(limits.extracted_max_bytes)
    rows, seen_paths = [], set()
    for info in members:
        parts = [p for p in info.filename.split("/") if p]
        if not has_image_extension(info.filename):
            report.warn(Code.UNSUPPORTED_EXTENSION, "ignored: not an image extension", info.filename)
            continue
        if len(parts) < 2:
            report.error(Code.NO_DATA, "image outside a class folder", info.filename)
            continue
        label = normalise_label(parts[0])
        path = "/".join([label, *parts[1:]])
        if path in seen_paths:  # two members that normalise (NFC, trimmed label) to one object path
            report.error(Code.ARCHIVE_DUPLICATE_PATH, "two archive members map to one object path", path)
            continue
        seen_paths.add(path)
        if image_too_large(info, info.filename, limits, report):
            continue
        data = budget.read(zf, info)
        if check_image(data, info.filename, limits, report, decode):
            sink(path, data)
            rows.append(image_row(path, data, label, None, split))
    check_label_collisions({r[Col.LABEL] for r in rows}, report)
    if not rows and not report.failed:
        report.error(Code.NO_DATA, "no images found")
    return rows
