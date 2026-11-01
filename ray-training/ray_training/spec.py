"""The train job spec: envelope plus the `kind: train` payload of docs/trainer-plan.md (core-api TrainSpec mirrors it).

The trainer keeps no preset table: base model, learning rate and preprocessing all come from the spec (core-api's task_catalog)."""
import json
from dataclasses import dataclass

from ray_training.constants import (
    DEFAULT_BATCH_SIZE, DEFAULT_EPOCHS, DEFAULT_IMAGE_SIZE, DEFAULT_SEED, SPEC_VERSION, JobKind, Preset, Resample, Task)


@dataclass(frozen=True)
class Preprocessing:
    """How an image becomes model input; core-api task_catalog.Preprocessing. Written to model.json as it was used."""

    mean: tuple[float, float, float]
    std: tuple[float, float, float]
    rescale: float
    resample: Resample


@dataclass(frozen=True)
class TrainSpec:
    job_id: str
    tenant_id: str
    cpu: float  # resources.cpu and memory_bytes size the Ray task
    memory_bytes: int
    max_runtime_s: int  # resources.max_runtime_s, 0 = none
    bucket: str
    prefix: str
    task: Task
    preset: Preset
    base_model: str
    base_revision: str | None
    index_url: str
    manifest_url: str  # presigned GET of a JSON object {index path: presigned GET of that image}
    version_id: str | None
    lakefs_commit: str | None
    epochs: int
    batch_size: int
    image_size: int
    learning_rate: float
    preprocessing: Preprocessing
    seed: int


def parse_spec(text: str) -> TrainSpec:
    doc = json.loads(text)
    if doc.get("spec_version") != SPEC_VERSION:
        raise ValueError(f"job spec version {doc.get('spec_version')!r} not supported, expected {SPEC_VERSION}")
    if doc.get("kind") != JobKind.TRAIN:
        raise ValueError(f"job kind {doc.get('kind')!r} is not run by ray_training")
    p, data = doc["payload"], doc["payload"]["data"]
    task, preset = Task(p["task"]), Preset(p["preset"])
    base, hp = p["base_model"], p.get("hyperparameters") or {}
    pre = p["preprocessing"]
    return TrainSpec(
        job_id=doc["job_id"], tenant_id=doc["tenant_id"],
        cpu=doc["resources"]["cpu"], memory_bytes=doc["resources"]["memory_bytes"],
        max_runtime_s=doc["resources"].get("max_runtime_s", 0),
        bucket=p["bucket"], prefix=p["prefix"], task=task, preset=preset,
        base_model=base["repo_id"], base_revision=base.get("revision"),
        index_url=data["index_url"], manifest_url=data["manifest_url"],
        version_id=data.get("version_id"), lakefs_commit=data.get("lakefs_commit"),
        epochs=hp.get("epochs", DEFAULT_EPOCHS), batch_size=hp.get("batch_size", DEFAULT_BATCH_SIZE),
        image_size=hp.get("image_size", DEFAULT_IMAGE_SIZE), learning_rate=p["learning_rate"],
        preprocessing=Preprocessing(tuple(pre["mean"]), tuple(pre["std"]), pre["rescale"], Resample(pre["resample"])),
        seed=p.get("seed", DEFAULT_SEED),
    )
