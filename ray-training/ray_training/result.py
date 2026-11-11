"""What a trainer hands back to the runner, and the pieces both trainers share."""
import random
from dataclasses import dataclass, field
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
    extra_files: dict[str, Path] = field(default_factory=dict)  # uploaded beside the model under this name (a tokenizer)


GIB = 1024**3


def device() -> torch.device:
    return torch.device("cuda" if torch.cuda.is_available() else "cpu")


def cap_gpu_share(gpu: float, vram_gb: float | None = None) -> bool:
    """A job may not use more GPU memory than it declared, so neighbours sharing the card are not starved by one job's allocator:
    its fraction of the card, or `vram_gb` when that is smaller. True when a cap was set (a whole GPU with no declaration, or the CPU, is left alone)."""
    if not torch.cuda.is_available() or gpu <= 0:
        return False
    share = gpu if gpu < 1 else 1.0
    if vram_gb:
        share = min(share, vram_gb * GIB / torch.cuda.get_device_properties(0).total_memory)
    if share >= 1:
        return False
    torch.cuda.set_per_process_memory_fraction(share)
    return True


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
    """Export an image model with a dynamic batch axis."""
    dummy = torch.zeros(1, 3, size, size, device=next(wrapper.parameters()).device)
    export_onnx_inputs(wrapper, (dummy,), [INPUT_NAME], outputs, path)


def export_onnx_inputs(wrapper: torch.nn.Module, dummies: tuple, inputs: list[str], outputs: list[str], path: Path) -> None:
    """Export with a dynamic batch axis. The legacy TorchScript exporter: the dynamo one rejects some HF modules."""
    wrapper.eval()
    torch.onnx.export(
        wrapper, dummies, str(path), input_names=inputs, output_names=outputs, opset_version=ONNX_OPSET,
        dynamic_axes={n: {0: "batch"} for n in [*inputs, *outputs]}, dynamo=False)
