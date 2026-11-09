"""The train job spec: envelope plus the `kind: train` payload of docs/trainer-plan.md (core-api TrainSpec mirrors it).

The trainer keeps no preset table: base model, learning rate and preprocessing all come from the spec (core-api's task_catalog)."""
import json
from dataclasses import dataclass

from ray_training.constants import (
    DEFAULT_BATCH_SIZE, DEFAULT_EPOCHS, DEFAULT_IMAGE_SIZE, DEFAULT_SEED, SPEC_VERSION, Augmentation, JobKind, LrSchedule,
    Resample)


@dataclass(frozen=True)
class Preprocessing:
    """How an image becomes model input; core-api task_catalog.Preprocessing. Written to model.json as it was used."""

    mean: tuple[float, float, float]
    std: tuple[float, float, float]
    rescale: float
    resample: Resample


@dataclass(frozen=True)
class TextPreprocessing:
    """How text becomes model input: the base model's tokenizer, padded and truncated to `max_length` tokens."""

    max_length: int


@dataclass(frozen=True)
class TrainSpec:
    job_id: str
    tenant_id: str
    cpu: float  # resources size each Ray Train worker
    memory_bytes: int
    gpu: float  # resources.gpu: the Ray Train workers, one GPU each (a fraction = one worker holding that share)
    max_runtime_s: int  # resources.max_runtime_s, 0 = none
    bucket: str
    prefix: str
    task: str  # a task_types slug; data.py looks its loader up by slug (LOADERS)
    trainer: str  # "module:function" of the function that trains it (registry.trainer_for)
    preset: str  # a task_presets name in core-api; only recorded, the spec carries the values it stood for
    base_model: str
    base_revision: str | None
    index_url: str | None  # images: presigned GET of index.parquet
    manifest_url: str | None  # images: presigned GET of a JSON object {index path: presigned GET of that image}
    data_url: str | None  # text: presigned GET of data.parquet
    version_id: str | None
    lakefs_commit: str | None
    epochs: int
    batch_size: int
    image_size: int
    learning_rate: float
    preprocessing: Preprocessing | TextPreprocessing
    seed: int
    lr_schedule: LrSchedule = LrSchedule.CONSTANT  # the preset's recipe, as the spec carries it; the defaults are the old behaviour
    warmup_fraction: float = 0.0
    augmentation: Augmentation = Augmentation.NONE
    early_stopping_patience: int = 0  # 0 = off
    events_url: str | None = None  # reporting.events_url: where to post progress; None = post nothing
    events_token_env: str | None = None  # reporting.token_env: the environment variable that holds the token (never in the spec)


def parse_preprocessing(pre: dict) -> Preprocessing | TextPreprocessing:
    if "max_length" in pre:
        return TextPreprocessing(pre["max_length"])
    return Preprocessing(tuple(pre["mean"]), tuple(pre["std"]), pre["rescale"], Resample(pre["resample"]))


def parse_spec(text: str) -> TrainSpec:
    doc = json.loads(text)
    if doc.get("spec_version") != SPEC_VERSION:
        raise ValueError(f"job spec version {doc.get('spec_version')!r} not supported, expected {SPEC_VERSION}")
    if doc.get("kind") != JobKind.TRAIN:
        raise ValueError(f"job kind {doc.get('kind')!r} is not run by ray_training")
    p, data = doc["payload"], doc["payload"]["data"]
    task, preset = p["task"], p["preset"]
    base, hp = p["base_model"], p.get("hyperparameters") or {}
    pre = p["preprocessing"]
    reporting = doc.get("reporting") or {}
    return TrainSpec(
        job_id=doc["job_id"], tenant_id=doc["tenant_id"],
        cpu=doc["resources"]["cpu"], memory_bytes=doc["resources"]["memory_bytes"], gpu=doc["resources"].get("gpu", 0),
        max_runtime_s=doc["resources"].get("max_runtime_s", 0),
        bucket=p["bucket"], prefix=p["prefix"], task=task, trainer=p["trainer"], preset=preset,
        base_model=base["repo_id"], base_revision=base.get("revision"),
        index_url=data.get("index_url"), manifest_url=data.get("manifest_url"), data_url=data.get("data_url"),
        version_id=data.get("version_id"), lakefs_commit=data.get("lakefs_commit"),
        epochs=hp.get("epochs", DEFAULT_EPOCHS), batch_size=hp.get("batch_size", DEFAULT_BATCH_SIZE),
        image_size=hp.get("image_size", DEFAULT_IMAGE_SIZE), learning_rate=p["learning_rate"],
        preprocessing=parse_preprocessing(pre),
        seed=p.get("seed", DEFAULT_SEED),
        lr_schedule=LrSchedule(p.get("lr_schedule", LrSchedule.CONSTANT)), warmup_fraction=p.get("warmup_fraction", 0.0),
        augmentation=Augmentation(p.get("augmentation", Augmentation.NONE)),
        early_stopping_patience=p.get("early_stopping_patience", 0),
        events_url=reporting.get("events_url"), events_token_env=reporting.get("token_env"),
    )
