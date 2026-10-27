"""Object detection: RT-DETRv2 (Apache-2.0, ADR-016) fine-tuned from the `boxes` column, exported to ONNX.

Boxes in index.parquet are pixels (x, y, width, height) of the original image; the model sees them as normalised
(cx, cy, w, h) of the resized square image, which is also what pred_boxes holds in the ONNX output.
"""
from pathlib import Path

import torch
from torch.utils.data import DataLoader, Dataset as TorchDataset
from transformers import RTDetrV2ForObjectDetection

from ray_training.data import Dataset, Sample, split_for_training
from ray_training.metrics import detection_metrics
from ray_training.result import (
    INPUT_NAME, NUM_WORKERS_GPU, Result, device, export_onnx, load_image, preprocessing, seed_everything)
from ray_training.spec import TrainSpec

MAX_DETECTIONS = 100  # kept per image when scoring


class Images(TorchDataset):
    def __init__(self, samples: list[Sample], size: int):
        self.samples, self.size = samples, size

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, i):
        image, w, h = load_image(self.samples[i].file, self.size)
        boxes = [[(x + bw / 2) / w, (y + bh / 2) / h, bw / w, bh / h] for _, x, y, bw, bh in self.samples[i].boxes]
        classes = [c for c, *_ in self.samples[i].boxes]
        return image, {"class_labels": torch.tensor(classes, dtype=torch.long), "boxes": torch.tensor(boxes, dtype=torch.float32).reshape(-1, 4)}


def collate(batch):
    return torch.stack([b[0] for b in batch]), [b[1] for b in batch]


def cxcywh_to_xyxy(b):
    return [b[0] - b[2] / 2, b[1] - b[3] / 2, b[0] + b[2] / 2, b[1] + b[3] / 2]


class Outputs(torch.nn.Module):
    """DETR-style outputs: logits (batch, queries, classes) and pred_boxes (batch, queries, 4) normalised cxcywh."""

    def __init__(self, model):
        super().__init__()
        self.model = model

    def forward(self, pixel_values):
        out = self.model(pixel_values=pixel_values)
        return out.logits, out.pred_boxes


def evaluate(model, loader, dev, num_classes: int) -> dict:
    model.eval()
    predictions, targets = [], []
    with torch.no_grad():
        for x, labels in loader:
            out = model(pixel_values=x.to(dev))
            scores = out.logits.sigmoid().flatten(1)  # focal-loss head: one sigmoid per (query, class)
            top, idx = scores.topk(min(MAX_DETECTIONS, scores.shape[1]), dim=1)
            for s, i, boxes, label in zip(top.cpu(), idx.cpu(), out.pred_boxes.cpu(), labels):
                predictions.append([(int(k % num_classes), float(sc), cxcywh_to_xyxy(boxes[k // num_classes].tolist())) for sc, k in zip(s, i)])
                targets.append([(int(c), cxcywh_to_xyxy(b.tolist())) for c, b in zip(label["class_labels"], label["boxes"])])
    return detection_metrics(predictions, targets, num_classes)


def train(spec: TrainSpec, ds: Dataset, work: Path) -> Result:
    seed_everything(spec.seed)
    dev = device()
    train_set, held_out = split_for_training(ds)
    n = len(ds.classes)
    model = RTDetrV2ForObjectDetection.from_pretrained(
        spec.base_model, revision=spec.base_revision, num_labels=n, id2label=dict(enumerate(ds.classes)),
        label2id={c: i for i, c in enumerate(ds.classes)}, ignore_mismatched_sizes=True).to(dev)
    workers = NUM_WORKERS_GPU if dev.type == "cuda" else 0
    train_loader = DataLoader(Images(train_set, spec.image_size), spec.batch_size, shuffle=True, collate_fn=collate, num_workers=workers)
    eval_loader = DataLoader(Images(held_out, spec.image_size), spec.batch_size, collate_fn=collate, num_workers=workers)
    optimizer = torch.optim.AdamW(model.parameters(), lr=spec.learning_rate)
    history = []
    for epoch in range(spec.epochs):
        model.train()
        total = 0.0
        for x, labels in train_loader:
            labels = [{k: v.to(dev) for k, v in label.items()} for label in labels]
            loss = model(pixel_values=x.to(dev), labels=labels).loss
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            total += loss.item() * len(labels)
        history.append({"epoch": epoch + 1, "train_loss": total / len(train_set), **evaluate(model, eval_loader, dev, n)})
    metrics = evaluate(model, eval_loader, dev, n)
    onnx = work / "model.onnx"
    export_onnx(Outputs(model), spec.image_size, ["logits", "pred_boxes"], onnx)
    queries = model.config.num_queries
    signature = {
        "inputs": [{"name": INPUT_NAME, "shape": ["batch", 3, spec.image_size, spec.image_size], "dtype": "float32"}],
        "outputs": [{"name": "logits", "shape": ["batch", queries, n], "dtype": "float32", "activation": "sigmoid"},
                    {"name": "pred_boxes", "shape": ["batch", queries, 4], "dtype": "float32", "format": "cxcywh_normalized"}],
        "preprocessing": preprocessing(spec.image_size)}
    return Result(ds.classes, metrics, history, onnx, signature)
