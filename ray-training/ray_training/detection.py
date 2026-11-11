"""Object detection: RT-DETRv2 (Apache-2.0, ADR-016) fine-tuned from the `boxes` column, exported to ONNX.

Boxes in index.parquet are pixels (x, y, width, height) of the original image; the model sees them as normalised
(cx, cy, w, h) of the resized square image, which is also what pred_boxes holds in the ONNX output.
"""
from pathlib import Path

import torch
from torch.utils.data import Dataset as TorchDataset
from transformers import RTDetrV2ForObjectDetection

from ray_training.constants import Augmentation
from ray_training.data import Dataset, Sample, split_for_training
from ray_training.loop import Kit, fit
from ray_training.metrics import detection_metrics
from ray_training.near_duplicates import count_near_duplicates
from ray_training.recipe import flip_with_boxes
from ray_training.result import INPUT_NAME, Result, export_onnx, load_image, preprocessing, seed_everything
from ray_training.spec import Preprocessing, TrainSpec

MAX_DETECTIONS = 100  # kept per image when scoring
MONITOR = "map50_95"  # what early stopping watches


class Images(TorchDataset):
    def __init__(self, samples: list[Sample], size: int, pre: Preprocessing, augment: Augmentation = Augmentation.NONE):
        self.samples, self.size, self.pre, self.augment = samples, size, pre, augment

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, i):
        image, w, h = load_image(self.samples[i].file, self.size, self.pre)
        boxes = [[(x + bw / 2) / w, (y + bh / 2) / h, bw / w, bh / h] for _, x, y, bw, bh in self.samples[i].boxes]
        classes = [c for c, *_ in self.samples[i].boxes]
        boxes = torch.tensor(boxes, dtype=torch.float32).reshape(-1, 4)
        if self.augment == Augmentation.FLIP:
            image, boxes = flip_with_boxes(image, boxes)
        return image, {"class_labels": torch.tensor(classes, dtype=torch.long), "boxes": boxes}


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


def evaluate(model, loader, dev) -> tuple[dict, None]:
    model.eval()
    num_classes = model.config.num_labels
    predictions, targets = [], []
    with torch.no_grad():
        for x, labels in loader:
            out = model(pixel_values=x.to(dev))
            scores = out.logits.sigmoid().flatten(1)  # focal-loss head: one sigmoid per (query, class)
            top, idx = scores.topk(min(MAX_DETECTIONS, scores.shape[1]), dim=1)
            for s, i, boxes, label in zip(top.cpu(), idx.cpu(), out.pred_boxes.cpu(), labels):
                predictions.append([(int(k % num_classes), float(sc), cxcywh_to_xyxy(boxes[k // num_classes].tolist())) for sc, k in zip(s, i)])
                targets.append([(int(c), cxcywh_to_xyxy(b.tolist())) for c, b in zip(label["class_labels"], label["boxes"])])
    return detection_metrics(predictions, targets, num_classes), None


def loss(model, batch, dev):
    x, labels = batch
    labels = [{k: v.to(dev) for k, v in label.items()} for label in labels]
    return model(pixel_values=x.to(dev), labels=labels).loss, len(labels)


def train(spec: TrainSpec, ds: Dataset, work: Path) -> Result | None:
    seed_everything(spec.seed)
    train_set, held_out = split_for_training(ds)
    n = len(ds.classes)
    model = RTDetrV2ForObjectDetection.from_pretrained(
        spec.base_model, revision=spec.base_revision, num_labels=n, id2label=dict(enumerate(ds.classes)),
        label2id={c: i for i, c in enumerate(ds.classes)}, ignore_mismatched_sizes=True)
    done = fit(Kit(model, Images(train_set, spec.image_size, spec.preprocessing, spec.augmentation), Images(held_out, spec.image_size, spec.preprocessing), collate, loss, evaluate, MONITOR), spec)
    if done is None:
        return None
    onnx = work / "model.onnx"
    export_onnx(Outputs(done.model), spec.image_size, ["logits", "pred_boxes"], onnx)
    queries = done.model.config.num_queries
    signature = {
        "inputs": [{"name": INPUT_NAME, "shape": ["batch", 3, spec.image_size, spec.image_size], "dtype": "float32"}],
        "outputs": [{"name": "logits", "shape": ["batch", queries, n], "dtype": "float32", "activation": "sigmoid"},
                    {"name": "pred_boxes", "shape": ["batch", queries, 4], "dtype": "float32", "format": "cxcywh_normalized"}],
        "preprocessing": preprocessing(spec)}
    return Result(ds.classes, done.metrics, done.history, onnx, signature, near_duplicates=count_near_duplicates(ds))
