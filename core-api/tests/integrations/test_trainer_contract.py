"""The contract between core-api and the training code: the task bundles in task-bundles/tasks/ (their manifest.json is what
register validates, their trainer has ray_core's signature) and ray_core's spec parser (it reads what a promoted preset puts
in the spec). These fail when core-api, ray_core and the bundles drift."""
import ast
import json
import uuid
from pathlib import Path

import pytest

from app.core.bundle_manifest import CORE_CONTRACT, BundleManifest, missing_entry_points
from app.core.presets import TaskPreset
from app.core.task_catalog import DataKind, Resample, TextPreprocessing
from app.integrations.job_spec import Resources, TrainData, TrainPayload, TrainSpec
from ray_core import constants as core
from ray_core.spec import parse_spec

BUNDLES = sorted((Path(__file__).parents[3] / "task-bundles" / "tasks").glob("*/manifest.json"))
TRAINER_PARAMS = ["spec", "work", "get"]  # ray_core.registry.Trainer


def _manifest(path: Path) -> BundleManifest:
    return BundleManifest.model_validate(json.loads(path.read_text()))


def _sources(path: Path) -> dict[str, bytes]:
    return {p.relative_to(path.parent).as_posix(): p.read_bytes() for p in path.parent.rglob("*.py") if "tests" not in p.parts}


def test_there_are_bundles_to_check():
    assert {p.parent.name for p in BUNDLES} >= {"image-classification", "object-detection", "text-classification", "tabular-classification"}


@pytest.mark.parametrize("path", BUNDLES, ids=lambda p: p.parent.name)
def test_every_bundle_manifest_is_one_register_accepts(path):
    manifest = _manifest(path)
    assert manifest.slug == path.parent.name and manifest.contract == CORE_CONTRACT == core.CONTRACT
    assert missing_entry_points(manifest, _sources(path)) == []


@pytest.mark.parametrize("path", BUNDLES, ids=lambda p: p.parent.name)
def test_every_bundle_trainer_has_the_core_signature(path):
    module, function = _manifest(path).trainer.split(":")
    source = _sources(path).get(f"{module.replace('.', '/')}.py")
    (node,) = [n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == function]
    assert [a.arg for a in node.args.args] == TRAINER_PARAMS


def test_the_core_mirrors_the_resample_enum():
    assert {r.value: r.name for r in core.Resample} == {r.value: r.name for r in Resample}


def _spec(manifest: BundleManifest, preset) -> TrainSpec:
    row = TaskPreset.model_validate({**preset.model_dump(), "task": manifest.slug, "updated_by": "t", "updated_at": "2026-01-01T00:00:00Z"})
    data = TrainData(version_id=uuid.uuid4(), lakefs_commit="c0ffee", **(
        {"data_url": "http://d"} if manifest.data_kind != DataKind.IMAGES else {"index_url": "http://i", "manifest_url": "http://m"}))
    payload = TrainPayload(
        bucket="b", prefix="p/", task=manifest.slug, trainer=manifest.trainer, data_kind=manifest.data_kind, preset=row.name,
        base_model=row.base_model, learning_rate=row.learning_rate, preprocessing=row.preprocessing, lr_schedule=row.lr_schedule,
        warmup_fraction=row.warmup_fraction, augmentation=row.augmentation, early_stopping_patience=row.early_stopping_patience,
        data=data, hyperparameters=row.hyperparameters, core_sha256="a" * 64, bundle_sha256="b" * 64)
    return TrainSpec(job_id="j", tenant_id=uuid.uuid4(), payload=payload, resources=Resources(cpu=row.cpu, memory_bytes=row.memory_bytes, gpu=row.gpu))


@pytest.mark.parametrize("path", BUNDLES, ids=lambda p: p.parent.name)
def test_the_core_reads_what_every_bundle_preset_puts_in_the_spec(path):
    manifest = _manifest(path)
    for preset in manifest.presets:
        spec = parse_spec(_spec(manifest, preset).model_dump_json())
        assert (spec.task, spec.trainer, spec.preset, spec.learning_rate) == (manifest.slug, manifest.trainer, preset.name, preset.learning_rate)
        assert spec.hyperparameters == preset.hyperparameters
        assert spec.base_model == (preset.base_model and preset.base_model.repo_id)
        if isinstance(preset.preprocessing, TextPreprocessing):
            assert spec.preprocessing.max_length == preset.preprocessing.max_length
        elif preset.preprocessing is None:
            assert spec.preprocessing is None
        else:
            assert spec.preprocessing.resample.value == preset.preprocessing.resample.value
