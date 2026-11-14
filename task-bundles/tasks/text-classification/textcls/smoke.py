"""The smoke job of this bundle: a 1-layer BERT and a word-level tokenizer built in place (no base model download) on 8 texts.

`spec_doc` is the manifest's `smoke`: ray_core.entrypoints.smoke calls it with {"job_id", "bucket", "prefix", ...} and runs the
train spec it returns through the real driver. The data travels in the spec as a `data:` URL, so any node can read it.
"""
import io
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from tokenizers import Tokenizer, models, pre_tokenizers
from transformers import BertConfig, BertForSequenceClassification, PreTrainedTokenizerFast

from ray_core.constants import LABEL_COLUMN, SPEC_VERSION, TEXT_COLUMN, Col, JobKind, Split
from ray_core.data import Get, load_text_dataset
from ray_core.result import Result
from ray_core.spec import TrainSpec
from ray_core.store import data_url
from textcls import train as text_classification

VOCAB = ["great", "good", "love", "awful", "bad", "hate", "movie", "film"]
PAD, UNK = "[PAD]", "[UNK]"
TEXTS = 8
VALIDATION_FROM = 6
MAX_LENGTH = 8
EPOCHS = 2  # more than one, so a checkpoint is written and read
BATCH_SIZE = 4
MAX_RUNTIME_S = 600
TRAINER = "textcls.smoke:train"
TASK = "text-classification"
MEMORY_BYTES = 1 << 30
LEARNING_RATE = 1e-3


def tiny(num_labels: int):
    """(model, tokenizer): what from_pretrained would give for a 1-layer BERT over VOCAB."""
    vocab = {w: i for i, w in enumerate([PAD, UNK, *VOCAB])}
    tk = Tokenizer(models.WordLevel(vocab, unk_token=UNK))
    tk.pre_tokenizer = pre_tokenizers.Whitespace()
    tokenizer = PreTrainedTokenizerFast(tokenizer_object=tk, unk_token=UNK, pad_token=PAD)
    model = BertForSequenceClassification(BertConfig(
        vocab_size=len(vocab), hidden_size=16, num_hidden_layers=1, num_attention_heads=2, intermediate_size=32,
        max_position_embeddings=64, pad_token_id=0, num_labels=num_labels))
    return model, tokenizer


def train(spec: TrainSpec, work: Path, get: Get) -> Result | None:
    ds = load_text_dataset(spec, get)
    model, tokenizer = tiny(len(ds.classes))
    return text_classification.train_model(spec, ds, work, model=model, tokenizer=tokenizer)


def fixture_data() -> dict:
    rows = []
    for i in range(TEXTS):
        good = i % 2 == 0
        rows.append({TEXT_COLUMN: f"{'great love' if good else 'awful hate'} {'movie' if i % 4 < 2 else 'film'}",
                     LABEL_COLUMN: "pos" if good else "neg", Col.SPLIT.value: (Split.VALIDATION if i >= VALIDATION_FROM else Split.TRAIN).value})
    sink = io.BytesIO()
    pq.write_table(pa.Table.from_pylist(rows), sink)
    return {"data_url": data_url(sink.getvalue())}


def spec_doc(arg: dict) -> dict:
    return {
        "spec_version": SPEC_VERSION, "kind": JobKind.TRAIN, "job_id": arg["job_id"], "tenant_id": "smoke",
        "resources": {"cpu": 1, "memory_bytes": MEMORY_BYTES, "gpu": 0, "max_runtime_s": MAX_RUNTIME_S},
        "payload": {
            "bucket": arg["bucket"], "prefix": arg["prefix"], "task": TASK, "trainer": TRAINER, "preset": "smoke",
            "base_model": {"repo_id": "smoke"}, "learning_rate": LEARNING_RATE, "preprocessing": {"max_length": MAX_LENGTH},
            "data": fixture_data(), "hyperparameters": {"epochs": EPOCHS, "batch_size": BATCH_SIZE}}}
