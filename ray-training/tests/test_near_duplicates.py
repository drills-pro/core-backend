"""count_near_duplicates: a held-out image that is a re-encoded or slightly noised copy of a training image is counted; a different picture is not."""
import numpy as np
from PIL import Image

from ray_core.constants import Split
from ray_core.data import Dataset, Sample
from ray_core.near_duplicates import count_near_duplicates


def picture(path, seed, noise=0):
    rng = np.random.default_rng(seed)
    base = np.kron(rng.integers(0, 255, (8, 8, 3)), np.ones((16, 16, 1))).astype(np.int16)  # 8x8 blocks of flat colour
    if noise:
        base = base + np.random.default_rng(99).integers(-noise, noise + 1, base.shape)
    Image.fromarray(np.clip(base, 0, 255).astype(np.uint8)).save(path)
    return path


def dataset(tmp_path, held_out_files):
    train = [Sample(picture(tmp_path / f"t{i}.png", i), Split.TRAIN, label=0) for i in range(5)]
    return Dataset(["a"], train + [Sample(f, Split.VALIDATION, label=0) for f in held_out_files])


def test_a_noised_copy_counts_and_a_different_picture_does_not(tmp_path):
    twin = picture(tmp_path / "twin.png", 2, noise=3)  # train image 2, plus noise
    other = picture(tmp_path / "other.png", 1000)
    assert count_near_duplicates(dataset(tmp_path, [twin, other])) == 1


def test_a_text_dataset_is_not_measured(tmp_path):
    ds = Dataset(["a"], [Sample(None, Split.TRAIN, label=0, text="x"), Sample(None, Split.VALIDATION, label=0, text="y")])
    assert count_near_duplicates(ds) is None
