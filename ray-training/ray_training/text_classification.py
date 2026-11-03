"""Text classification: a Hugging Face sequence classifier (BERT family) fine-tuned on data.parquet, exported to ONNX.

Every text is padded to `max_length` tokens, so the exported graph has a fixed sequence length and a dynamic batch.
The tokenizer is uploaded beside the model (tokenizer.json) and is what serving tokenizes with.
"""
from pathlib import Path

import numpy as np
import torch
from torch.utils.data import Dataset as TorchDataset
from transformers import AutoModelForSequenceClassification, AutoTokenizer

from ray_training.constants import CONFUSION_TOP_K, TOKENIZER_KEY
from ray_training.data import Dataset, Sample, split_for_training
from ray_training.loop import Kit, fit
from ray_training.metrics import classification_metrics
from ray_training.result import Result, export_onnx_inputs, seed_everything
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


def evaluate(model, loader, dev) -> tuple[dict, list[list[int]]]:
    model.eval()
    logits, targets = [], []
    with torch.no_grad():
        for ids, mask, y in loader:
            logits.append(model(input_ids=ids.to(dev), attention_mask=mask.to(dev)).logits.cpu().numpy())
            targets.append(y.numpy())
    return classification_metrics(np.concatenate(logits), np.concatenate(targets), model.config.num_labels)


def loss(model, batch, dev):
    ids, mask, y = batch
    return model(input_ids=ids.to(dev), attention_mask=mask.to(dev), labels=y.to(dev)).loss, len(y)


def train(spec: TrainSpec, ds: Dataset, work: Path) -> Result | None:
    seed_everything(spec.seed)
    pre: TextPreprocessing = spec.preprocessing
    train_set, held_out = split_for_training(ds)
    n = len(ds.classes)
    tokenizer = AutoTokenizer.from_pretrained(spec.base_model, revision=spec.base_revision)
    model = AutoModelForSequenceClassification.from_pretrained(
        spec.base_model, revision=spec.base_revision, num_labels=n, id2label=dict(enumerate(ds.classes)),
        label2id={c: i for i, c in enumerate(ds.classes)}, ignore_mismatched_sizes=True)
    done = fit(Kit(model, Texts(train_set), Texts(held_out), collator(tokenizer, pre), loss, evaluate), spec)
    if done is None:
        return None
    onnx, tokenizer_file = work / "model.onnx", work / TOKENIZER_KEY
    dummy = torch.zeros(1, pre.max_length, dtype=torch.long, device=next(done.model.parameters()).device)
    export_onnx_inputs(Logits(done.model), (dummy, dummy), [INPUT_IDS, ATTENTION_MASK], ["logits"], onnx)
    backend = tokenizer.backend_tokenizer
    backend.no_padding()  # the collator switched these on; serving sets them from model.json
    backend.no_truncation()
    backend.save(str(tokenizer_file))
    signature = {
        "inputs": [{"name": name, "shape": ["batch", pre.max_length], "dtype": "int64"} for name in (INPUT_IDS, ATTENTION_MASK)],
        "outputs": [{"name": "logits", "shape": ["batch", n], "dtype": "float32"}],
        "preprocessing": {"max_length": pre.max_length, "tokenizer": TOKENIZER_KEY}, "top_k": min(CONFUSION_TOP_K, n)}
    return Result(ds.classes, done.metrics, done.history, onnx, signature, {"labels": ds.classes, "matrix": done.confusion}, {TOKENIZER_KEY: tokenizer_file})
