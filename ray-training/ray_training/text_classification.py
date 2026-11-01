"""Text classification: a Hugging Face sequence classifier (BERT family) fine-tuned on data.parquet, exported to ONNX.

Every text is padded to `max_length` tokens, so the exported graph has a fixed sequence length and a dynamic batch.
The tokenizer is uploaded beside the model (tokenizer.json) and is what serving tokenizes with.
"""
from pathlib import Path

import numpy as np
import torch
from torch.utils.data import DataLoader, Dataset as TorchDataset
from transformers import AutoModelForSequenceClassification, AutoTokenizer

from ray_training.constants import CONFUSION_TOP_K, TOKENIZER_KEY
from ray_training.data import Dataset, Sample, split_for_training
from ray_training.metrics import classification_metrics
from ray_training.result import Result, device, export_onnx_inputs, seed_everything
from ray_training.spec import TextPreprocessing, TrainSpec

INPUT_IDS, ATTENTION_MASK = "input_ids", "attention_mask"


class Texts(TorchDataset):
    def __init__(self, samples: list[Sample]):
        self.samples = samples

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, i):
        return self.samples[i].text, self.samples[i].label


def collator(tokenizer, pre: TextPreprocessing):
    def collate(batch):
        texts, labels = zip(*batch)
        enc = tokenizer(list(texts), truncation=True, padding="max_length", max_length=pre.max_length, return_tensors="pt")
        return enc[INPUT_IDS], enc[ATTENTION_MASK], torch.tensor(labels, dtype=torch.long)

    return collate


class Logits(torch.nn.Module):
    def __init__(self, model):
        super().__init__()
        self.model = model

    def forward(self, input_ids, attention_mask):
        return self.model(input_ids=input_ids, attention_mask=attention_mask).logits


def evaluate(model, loader, dev, num_classes: int) -> tuple[dict, list[list[int]]]:
    model.eval()
    logits, targets = [], []
    with torch.no_grad():
        for ids, mask, y in loader:
            logits.append(model(input_ids=ids.to(dev), attention_mask=mask.to(dev)).logits.cpu().numpy())
            targets.append(y.numpy())
    return classification_metrics(np.concatenate(logits), np.concatenate(targets), num_classes)


def train(spec: TrainSpec, ds: Dataset, work: Path) -> Result:
    seed_everything(spec.seed)
    dev = device()
    pre: TextPreprocessing = spec.preprocessing
    train_set, held_out = split_for_training(ds)
    n = len(ds.classes)
    tokenizer = AutoTokenizer.from_pretrained(spec.base_model, revision=spec.base_revision)
    model = AutoModelForSequenceClassification.from_pretrained(
        spec.base_model, revision=spec.base_revision, num_labels=n, id2label=dict(enumerate(ds.classes)),
        label2id={c: i for i, c in enumerate(ds.classes)}, ignore_mismatched_sizes=True).to(dev)
    collate = collator(tokenizer, pre)
    train_loader = DataLoader(Texts(train_set), spec.batch_size, shuffle=True, collate_fn=collate)
    eval_loader = DataLoader(Texts(held_out), spec.batch_size, collate_fn=collate)
    optimizer = torch.optim.AdamW(model.parameters(), lr=spec.learning_rate)
    history = []
    for epoch in range(spec.epochs):
        model.train()
        total = 0.0
        for ids, mask, y in train_loader:
            loss = model(input_ids=ids.to(dev), attention_mask=mask.to(dev), labels=y.to(dev)).loss
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            total += loss.item() * len(y)
        metrics, _ = evaluate(model, eval_loader, dev, n)
        history.append({"epoch": epoch + 1, "train_loss": total / len(train_set), **metrics})
    metrics, matrix = evaluate(model, eval_loader, dev, n)
    onnx, tokenizer_file = work / "model.onnx", work / TOKENIZER_KEY
    dummy = torch.zeros(1, pre.max_length, dtype=torch.long, device=dev)
    export_onnx_inputs(Logits(model), (dummy, dummy), [INPUT_IDS, ATTENTION_MASK], ["logits"], onnx)
    backend = tokenizer.backend_tokenizer
    backend.no_padding()  # the collator switched these on; serving sets them from model.json
    backend.no_truncation()
    backend.save(str(tokenizer_file))
    signature = {
        "inputs": [{"name": name, "shape": ["batch", pre.max_length], "dtype": "int64"} for name in (INPUT_IDS, ATTENTION_MASK)],
        "outputs": [{"name": "logits", "shape": ["batch", n], "dtype": "float32"}],
        "preprocessing": {"max_length": pre.max_length, "tokenizer": TOKENIZER_KEY}, "top_k": min(CONFUSION_TOP_K, n)}
    return Result(ds.classes, metrics, history, onnx, signature, {"labels": ds.classes, "matrix": matrix}, {TOKENIZER_KEY: tokenizer_file})
