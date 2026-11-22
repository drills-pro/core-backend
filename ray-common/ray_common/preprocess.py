"""How an input becomes model feeds, written once for the trainer (ray_core) and the serving replica (ray_serving.inputs), so they cannot
drift. Pure numpy and Pillow, no torch: import it where it is used, never from a module the ingest job loads."""
from collections.abc import Sequence

import numpy as np
from PIL import Image

UNSEEN = np.nan  # a missing value, and a category the model never saw


def resampling(name: str) -> Image.Resampling:
    """The Pillow filter for a model.json `resample` ("bilinear", "bicubic"): one resolution for training and serving."""
    return Image.Resampling[str(name).upper()]


def image_to_chw(image: Image.Image, size: tuple[int, int], resample: str, rescale: float, mean: Sequence[float], std: Sequence[float]) -> np.ndarray:
    """A float32 CHW array: the image as RGB, resized to `size` (width, height), scaled by `rescale`, then normalised."""
    rgb = image if image.mode == "RGB" else image.convert("RGB")  # convert copies, and a caller's image may be 25 megapixels
    resized = rgb.resize(size, resampling(resample))
    arr = (np.asarray(resized, dtype=np.float32) * rescale - np.array(mean, dtype=np.float32)) / np.array(std, dtype=np.float32)
    return arr.transpose(2, 0, 1)


def category_codes(categories: dict[str, list[str]]) -> dict[str, dict[str, int]]:
    """{column: {value: index in the column's sorted values}}."""
    return {name: {v: i for i, v in enumerate(values)} for name, values in categories.items()}


def tabular_cell(column: str, value, codes: dict[str, dict[str, int]]) -> float:
    """One feature value: None is missing (NaN), a category column's text is its code (a text the model never saw is missing),
    anything else is a number (a value that is not one raises, which serving reports as bad input)."""
    if value is None:
        return UNSEEN
    return codes[column].get(str(value), UNSEEN) if column in codes else float(value)


def tabular_matrix(rows: Sequence[Sequence], features: list[str], categories: dict[str, list[str]]) -> np.ndarray:
    """Rows of raw values, one per feature in order, as a float32 matrix."""
    codes = category_codes(categories)
    return np.array([[tabular_cell(c, v, codes) for c, v in zip(features, row)] for row in rows], dtype=np.float32)
