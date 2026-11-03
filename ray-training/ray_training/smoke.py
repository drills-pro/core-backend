"""The trainer of the smoke job: classification of a tiny linear model, so it needs no base model download."""
from pathlib import Path
from types import SimpleNamespace

import torch

from ray_training import classification
from ray_training.data import Dataset
from ray_training.result import Result
from ray_training.spec import TrainSpec

CHANNELS = 3


class Tiny(torch.nn.Module):
    """Answers like a Hugging Face image classifier: `.config.num_labels`, forward(pixel_values, labels) -> .logits, .loss."""

    def __init__(self, image_size: int, num_labels: int):
        super().__init__()
        self.config = SimpleNamespace(num_labels=num_labels)
        self.fc = torch.nn.Linear(CHANNELS * image_size * image_size, num_labels)

    def forward(self, pixel_values, labels=None):
        logits = self.fc(pixel_values.flatten(1))
        return SimpleNamespace(logits=logits, loss=None if labels is None else torch.nn.functional.cross_entropy(logits, labels))


def train(spec: TrainSpec, ds: Dataset, work: Path) -> Result | None:
    return classification.train_model(spec, ds, work, model=Tiny(spec.image_size, len(ds.classes)))
