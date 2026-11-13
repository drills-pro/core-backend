"""A trainer made of ray_core alone, standing in for a task bundle in the core tests: a linear classifier over the images of
index.parquet, trained by loop.fit and exported like a bundle would. Spec name: "tests.tiny:train"."""
from pathlib import Path

import numpy as np
import torch

from ray_core.data import load_image_dataset, split_for_training
from ray_core.loop import Kit, fit
from ray_core.metrics import classification_metrics
from ray_core.result import INPUT_NAME, Result, export_onnx, load_image, seed_everything

TRAINER = "tests.tiny:train"
CHANNELS = 3


class Linear(torch.nn.Module):
    def __init__(self, size: int, classes: int):
        super().__init__()
        self.fc = torch.nn.Linear(CHANNELS * size * size, classes)

    def forward(self, x):
        return self.fc(x.flatten(1))


def loss(model, batch, dev):
    x, y = batch
    return torch.nn.functional.cross_entropy(model(x.to(dev)), y.to(dev)), len(y)


def evaluate(model, loader, dev):
    model.eval()
    logits, targets = [], []
    with torch.no_grad():
        for x, y in loader:
            logits.append(model(x.to(dev)).cpu().numpy())
            targets.append(y.numpy())
    return classification_metrics(np.concatenate(logits), np.concatenate(targets), model.fc.out_features)


def train(spec, work: Path, get) -> Result | None:
    seed_everything(spec.seed)
    ds = load_image_dataset(spec, work, get, detection=False)
    train_set, held_out = split_for_training(ds)
    as_items = lambda samples: [(load_image(s.file, spec.image_size, spec.preprocessing)[0], s.label) for s in samples]  # noqa: E731
    done = fit(Kit(Linear(spec.image_size, len(ds.classes)), as_items(train_set), as_items(held_out), None, loss, evaluate), spec)
    if done is None:
        return None
    onnx = work / "model.onnx"
    export_onnx(done.model, spec.image_size, ["logits"], onnx)
    signature = {"inputs": [{"name": INPUT_NAME}], "outputs": [{"name": "logits"}]}
    return Result(ds.classes, done.metrics, done.history, onnx, signature, {"labels": ds.classes, "matrix": done.confusion})
