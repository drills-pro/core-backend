"""Dataset loading: index.parquet and a manifest of presigned object URLs -> local files, labelled and split."""
import hashlib
import io
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ray_training.constants import (
    HASH_HEX_CHARS, LABEL_COLUMN, SPLIT_BUCKETS, SPLIT_TRAIN_BELOW, SPLIT_VALIDATION_BELOW, TEXT_COLUMN, Box, Col, Split, Task)
from ray_training.errors import BadData
from ray_training.spec import TrainSpec
from ray_training.store import http_get

DOWNLOAD_THREADS = 16
Get = Callable[[str], bytes]


@dataclass
class Sample:
    file: Path | None  # images; None for a text sample
    split: Split
    label: int | None = None  # classification: index into Dataset.classes
    text: str | None = None  # text classification
    boxes: list[tuple[int, float, float, float, float]] = field(default_factory=list)  # (class, x, y, w, h) in pixels


@dataclass
class Dataset:
    classes: list[str]
    samples: list[Sample]

    def split(self, split: Split) -> list[Sample]:
        return [s for s in self.samples if s.split == split]


def hash_split(sha256: str) -> Split:
    """Deterministic 80/10/10 split from the content hash, stable across runs and dataset versions."""
    bucket = int(sha256[:HASH_HEX_CHARS], 16) % SPLIT_BUCKETS
    return Split.TRAIN if bucket < SPLIT_TRAIN_BELOW else Split.VALIDATION if bucket < SPLIT_VALIDATION_BELOW else Split.TEST


def read_index(data: bytes) -> list[dict]:
    import pyarrow.parquet as pq

    return pq.read_table(io.BytesIO(data)).to_pylist()


def load_text_dataset(spec: TrainSpec, get: Get) -> Dataset:
    """data.parquet: one row per text, the text in TEXT_COLUMN and its class in LABEL_COLUMN. A row with no split is
    assigned one from the hash of its text, like images."""
    rows = read_index(get(spec.data_url))
    rows = [r for r in rows if r.get(TEXT_COLUMN) is not None and r.get(LABEL_COLUMN) is not None]
    if not rows:
        raise BadData("data.parquet has no labelled text")
    classes = sorted({str(r[LABEL_COLUMN]) for r in rows})
    ids = {c: i for i, c in enumerate(classes)}
    samples = [
        Sample(None, Split(r[Col.SPLIT]) if r.get(Col.SPLIT) else hash_split(hashlib.sha256(r[TEXT_COLUMN].encode()).hexdigest()),
               ids[str(r[LABEL_COLUMN])], r[TEXT_COLUMN])
        for r in rows]
    return Dataset(classes, samples)


def load_dataset(spec: TrainSpec, work: Path, get: Get = http_get) -> Dataset:
    if spec.task == Task.TEXT_CLASSIFICATION:
        return load_text_dataset(spec, get)
    rows = read_index(get(spec.index_url))
    if not rows:
        raise BadData("index.parquet has no rows")
    objects = json.loads(get(spec.manifest_url))
    detection = spec.task == Task.OBJECT_DETECTION
    classes = sorted({b[Box.CATEGORY] for r in rows for b in r[Col.BOXES] or []} if detection
                     else {r[Col.LABEL] for r in rows if r[Col.LABEL] is not None})
    if not classes:
        raise BadData("the dataset has no labels" if not detection else "the dataset has no boxes")
    ids = {c: i for i, c in enumerate(classes)}
    missing = [r[Col.PATH] for r in rows if r[Col.PATH] not in objects]
    if missing:
        raise BadData(f"{len(missing)} images have no URL in the manifest, first: {missing[0]}")
    images = work / "images"
    images.mkdir(parents=True, exist_ok=True)

    def fetch(i_row):
        i, row = i_row
        file = images / f"{i}{Path(row[Col.PATH]).suffix}"
        file.write_bytes(get(objects[row[Col.PATH]]))
        return file

    with ThreadPoolExecutor(DOWNLOAD_THREADS) as pool:
        files = list(pool.map(fetch, enumerate(rows)))
    samples = []
    for file, r in zip(files, rows):
        split = Split(r[Col.SPLIT]) if r[Col.SPLIT] else hash_split(r[Col.SHA256] or hashlib.sha256(file.read_bytes()).hexdigest())
        if detection:
            boxes = [(ids[b[Box.CATEGORY]], b[Box.X], b[Box.Y], b[Box.WIDTH], b[Box.HEIGHT]) for b in r[Col.BOXES] or []]
            samples.append(Sample(file, split, boxes=boxes))
        elif r[Col.LABEL] is not None:
            samples.append(Sample(file, split, label=ids[r[Col.LABEL]]))
    return Dataset(classes, samples)


def split_for_training(ds: Dataset) -> tuple[list[Sample], list[Sample]]:
    """(train, eval): evaluation uses `validation`, else `test`; an empty side is bad data, not a crash."""
    train = ds.split(Split.TRAIN)
    held_out = ds.split(Split.VALIDATION) or ds.split(Split.TEST)
    if not train or not held_out:
        raise BadData(f"need train and validation/test images, got {len(train)} train and {len(held_out)} held out")
    return train, held_out
