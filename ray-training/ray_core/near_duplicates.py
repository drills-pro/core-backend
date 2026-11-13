"""Held-out images that also sit, near enough, in the training set: a score measured on them flatters the model.

A difference hash (dHash) per image, compared by Hamming distance. Image tasks only; a text or unreadable sample is skipped.
"""
import numpy as np
from PIL import Image

from ray_core.data import Dataset, Sample, split_for_training

HASH_SIDE = 8  # a HASH_SIDE x HASH_SIDE grid of left-to-right brightness comparisons = 64 bits
MAX_DISTANCE = 4  # bits that may differ for two images to count as the same picture
MAX_CHECKED = 2000  # held-out images checked (the first ones, by file order): the work is checked x train


def dhash(file) -> int:
    with Image.open(file) as im:
        px = np.asarray(im.convert("L").resize((HASH_SIDE + 1, HASH_SIDE), Image.BILINEAR), dtype=np.int16)
    bits = (px[:, 1:] > px[:, :-1]).flatten()
    return int("".join("1" if b else "0" for b in bits), 2)


def _hashes(samples: list[Sample]) -> np.ndarray:
    out = []
    for s in samples:
        try:
            out.append(dhash(s.file))
        except Exception:  # noqa: BLE001 - one unreadable file must not hide the rest
            continue
    return np.array(out, dtype=np.uint64)


def _popcount(x: np.ndarray) -> np.ndarray:
    return np.unpackbits(x.view(np.uint8).reshape(len(x), 8), axis=1).sum(axis=1)


def count_near_duplicates(ds: Dataset) -> int | None:
    """How many of the (first MAX_CHECKED) held-out images have a training image within MAX_DISTANCE bits; None when it cannot be told."""
    try:
        train, held_out = split_for_training(ds)
        if any(s.file is None for s in train + held_out):
            return None
        train_hashes = _hashes(train)
        return sum(1 for h in _hashes(held_out[:MAX_CHECKED]) if len(train_hashes) and _popcount(train_hashes ^ h).min() <= MAX_DISTANCE)
    except Exception:  # noqa: BLE001 - a warning is never worth failing a run for
        return None
