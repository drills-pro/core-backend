"""Image classification: a Hugging Face image classifier (ConvNeXt, ViT) fine-tuned on index.parquet, exported to ONNX.

ponytail: transformers only; timm checkpoints load through its own hub integration when a preset needs one.
"""
from pathlib import Path

import numpy as np
import torch
from torch.utils.data import DataLoader, Dataset as TorchDataset
from transformers import AutoModelForImageClassification

from ray_training.constants import CONFUSION_TOP_K
from ray_training.data import Dataset, Sample, split_for_training
from ray_training.metrics import classification_metrics
from ray_training.result import (
    INPUT_NAME, NUM_WORKERS_GPU, Result, device, export_onnx, load_image, preprocessing, seed_everything)
from ray_training.spec import Preprocessing, TrainSpec


class Images(TorchDataset):
    def __init__(self, samples: list[Sample], size: int, pre: Preprocessing):
        self.samples, self.size, self.pre = samples, size, pre

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, i):
        return load_image(self.samples[i].file, self.size, self.pre)[0], self.samples[i].label


class Logits(torch.nn.Module):
    def __init__(self, model):
        super().__init__()
        self.model = model

    def forward(self, pixel_values):
        return self.model(pixel_values=pixel_values).logits


def evaluate(model, loader, dev, num_classes: int) -> tuple[dict, list[list[int]]]:
    model.eval()
    logits, targets = [], []
    with torch.no_grad():
        for x, y in loader:
            logits.append(model(pixel_values=x.to(dev)).logits.cpu().numpy())
            targets.append(y.numpy())
    return classification_metrics(np.concatenate(logits), np.concatenate(targets), num_classes)


def train(spec: TrainSpec, ds: Dataset, work: Path) -> Result:
    seed_everything(spec.seed)
    dev = device()
    train_set, held_out = split_for_training(ds)
    n = len(ds.classes)
    model = AutoModelForImageClassification.from_pretrained(
        spec.base_model, revision=spec.base_revision, num_labels=n, id2label=dict(enumerate(ds.classes)),
        label2id={c: i for i, c in enumerate(ds.classes)}, ignore_mismatched_sizes=True).to(dev)
    workers = NUM_WORKERS_GPU if dev.type == "cuda" else 0
    train_loader = DataLoader(Images(train_set, spec.image_size, spec.preprocessing), spec.batch_size, shuffle=True, num_workers=workers)
    eval_loader = DataLoader(Images(held_out, spec.image_size, spec.preprocessing), spec.batch_size, num_workers=workers)
    optimizer = torch.optim.AdamW(model.parameters(), lr=spec.learning_rate)
    history = []
    for epoch in range(spec.epochs):
        model.train()
        total = 0.0
        for x, y in train_loader:
            loss = model(pixel_values=x.to(dev), labels=y.to(dev)).loss
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            total += loss.item() * len(y)
        metrics, _ = evaluate(model, eval_loader, dev, n)
        history.append({"epoch": epoch + 1, "train_loss": total / len(train_set), **metrics})
    metrics, matrix = evaluate(model, eval_loader, dev, n)
    onnx = work / "model.onnx"
    export_onnx(Logits(model), spec.image_size, ["logits"], onnx)
    signature = {
        "inputs": [{"name": INPUT_NAME, "shape": ["batch", 3, spec.image_size, spec.image_size], "dtype": "float32"}],
        "outputs": [{"name": "logits", "shape": ["batch", n], "dtype": "float32"}],
        "preprocessing": preprocessing(spec), "top_k": min(CONFUSION_TOP_K, n)}
    return Result(ds.classes, metrics, history, onnx, signature, {"labels": ds.classes, "matrix": matrix})
