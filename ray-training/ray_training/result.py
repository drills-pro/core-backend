"""What a trainer hands back to the runner, and the pieces both trainers share."""
import random
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import torch
from PIL import Image

from ray_training.constants import ONNX_OPSET
from ray_training.spec import Preprocessing, TrainSpec

INPUT_NAME = "pixel_values"
NUM_WORKERS_GPU = 4  # data loading workers when a GPU waits on them; 0 on CPU (CI)


@dataclass
class Result:
    classes: list[str]
    metrics: dict
    history: list[dict]  # one dict per epoch
    onnx: Path
    signature: dict  # model.json: inputs, outputs, preprocessing
    confusion: dict | None = None  # classification: {"labels": [...], "matrix": [[...]]}


def device() -> torch.device:
    return torch.device("cuda" if torch.cuda.is_available() else "cpu")


def seed_everything(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)


def load_image(file: Path, size: int, pre: Preprocessing) -> tuple[torch.Tensor, int, int]:
    """(CHW float tensor resized to size x size and normalised by `pre`, original width, original height)."""
    with Image.open(file) as im:
        im = im.convert("RGB")
        w, h = im.size
        arr = np.asarray(im.resize((size, size), Image.Resampling[pre.resample.name]), dtype=np.float32) * pre.rescale
    arr = (arr - np.array(pre.mean, dtype=np.float32)) / np.array(pre.std, dtype=np.float32)
    return torch.from_numpy(arr).permute(2, 0, 1).contiguous(), w, h


def preprocessing(spec: TrainSpec) -> dict:
    """The model.json entry: exactly what load_image applied, for whatever serves the model."""
    pre = spec.preprocessing
    return {
        "resize": [spec.image_size, spec.image_size], "resample": pre.resample.value, "rescale": pre.rescale,
        "mean": list(pre.mean), "std": list(pre.std), "layout": "NCHW", "color": "RGB"}


def export_onnx(wrapper: torch.nn.Module, size: int, outputs: list[str], path: Path) -> None:
    """Export with a dynamic batch axis. The legacy TorchScript exporter: the dynamo one rejects some HF modules."""
    wrapper.eval()
    dummy = torch.zeros(1, 3, size, size, device=next(wrapper.parameters()).device)
    torch.onnx.export(
        wrapper, (dummy,), str(path), input_names=[INPUT_NAME], output_names=outputs, opset_version=ONNX_OPSET,
        dynamic_axes={n: {0: "batch"} for n in [INPUT_NAME, *outputs]}, dynamo=False)
