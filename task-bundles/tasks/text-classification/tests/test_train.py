"""One epoch on the tiny fixtures, on CPU: report ok, metrics present, ONNX loads in onnxruntime, the tokenizer ships with it."""
import json
import re
from pathlib import Path

import jsonschema
import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

from ray_core.constants import MODEL_KEY, MODEL_MANIFEST_KEY, TOKENIZER_KEY
from ray_core.runner import run
from ray_core.spec import parse_spec
from tests.support import MAX_LENGTH, REPORT_SCHEMA, MemStore, text_get, text_rows, text_spec_text

SRC = Path(__file__).parents[1] / "textcls"


def test_text_classification_trains_exports_and_reports(tiny_bert):
    store = MemStore()
    report = run(parse_spec(text_spec_text(tiny_bert)), store, text_get(text_rows()))
    assert report["status"] == "succeeded", report["message"]
    jsonschema.validate(report, REPORT_SCHEMA)
    assert set(report["metrics"]) == {"top1", "top5"} and report["confusion_matrix"]["labels"] == ["neg", "pos"]
    onnx = ort.InferenceSession(store.puts[MODEL_KEY])
    ids = np.zeros((3, MAX_LENGTH), dtype=np.int64)  # dynamic batch axis
    (logits,) = onnx.run(None, {"input_ids": ids, "attention_mask": np.ones_like(ids)})
    assert logits.shape == (3, 2)
    manifest = json.loads(store.puts[MODEL_MANIFEST_KEY])
    assert manifest["classes"] == ["neg", "pos"] and manifest["preprocessing"] == {"max_length": MAX_LENGTH, "tokenizer": TOKENIZER_KEY, "pad_id": 0, "pad_token": "[PAD]"}
    tokenizer = Tokenizer.from_str(store.puts[TOKENIZER_KEY].decode())  # what serving tokenizes with
    assert tokenizer.encode("great movie").ids == [tokenizer.token_to_id("great"), tokenizer.token_to_id("movie")]


def test_every_from_pretrained_of_a_model_refuses_pickled_weights():
    calls = re.findall(r"ForSequenceClassification\.from_pretrained\((.*?)\)\n", (SRC / "train.py").read_text(), re.S)
    assert calls and all("use_safetensors=True" in c for c in calls)
