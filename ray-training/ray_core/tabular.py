"""What the tabular bundles (classification, regression) share: which columns are features, how a row becomes a feature vector,
and how rows are split. A bundle's own zip carries no copy of it; model.json records what serving needs to encode the same way."""
import hashlib
import json

import numpy as np

from ray_common.preprocess import tabular_matrix
from ray_core.constants import Col, Split
from ray_core.data import carve_held_out, hash_split
from ray_core.errors import BadData

NUMERIC = (bool, int, float)
MAX_CATEGORIES = 50  # distinct values up to which a string column is a category
MAX_CATEGORY_SHARE = 0.5  # ... and at most this share of the rows: a column of mostly different values is an id or free text


def row_split(row: dict) -> Split:
    if row.get(Col.SPLIT):
        return Split(row[Col.SPLIT])
    return hash_split(hashlib.sha256(json.dumps(row, sort_keys=True, default=str).encode()).hexdigest())


def split_rows(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """(train, held out): `validation`, else `test`, else a strided fifth of the training rows (data.carve_held_out); bad data when a side is empty."""
    splits = [row_split(r) for r in rows]
    train = [r for r, s in zip(rows, splits) if s == Split.TRAIN]
    held_out = [r for r, s in zip(rows, splits) if s == Split.VALIDATION] or [r for r, s in zip(rows, splits) if s == Split.TEST]
    train, held_out = carve_held_out(train, held_out)
    if not train or not held_out:
        raise BadData(f"need train and validation/test rows, got {len(train)} train and {len(held_out)} held out")
    return train, held_out


def features_of(rows: list[dict], target: str) -> tuple[list[str], dict[str, list[str]]]:
    """(feature columns in table order, the sorted values of each category column). A feature column holds only numbers, bools,
    or missing values; or only strings, few enough to be categories (a string column becomes the index of its value in the sorted
    values). The target and the split are not features; an id or free-text column is dropped."""
    features, categories = [], {}
    for c in (c for c in rows[0] if c not in (target, Col.SPLIT)):
        values = [r[c] for r in rows if r[c] is not None]
        if all(isinstance(v, NUMERIC) for v in values):
            features.append(c)
        elif all(isinstance(v, str) for v in values) and len(set(values)) <= min(MAX_CATEGORIES, MAX_CATEGORY_SHARE * len(rows)):
            features.append(c)
            categories[c] = sorted(set(values))
    return features, categories


def matrix(rows: list[dict], features: list[str], categories: dict[str, list[str]]) -> np.ndarray:
    """Rows -> float32 feature matrix; a missing value is NaN (ray_common: serving encodes a row the same way)."""
    return tabular_matrix([[r[c] for c in features] for r in rows], features, categories)
