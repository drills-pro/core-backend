"""task_presets: what an admin may write, and how a row becomes a TaskPreset."""
import pytest
from pydantic import ValidationError

from app.core.bundle_manifest import ManifestPreset
from app.core.enums import TaskType
from app.core.presets import PresetBody
from app.core.recipe import Recipe
from app.core.settings import settings
from app.core.task_catalog import TextPreprocessing
from app.integrations.job_spec import Resources, TrainPayload
from app.modules.user.training.train_spec import _SPEC_OWN
from tests.support.presets import IMAGENET, LIMITS, REVISION, preset

BODY = {
    "base_model": {"repo_id": "org/model", "revision": REVISION}, "learning_rate": 1e-4, "preprocessing": IMAGENET,
    "cpu": 4, "memory_bytes": 1, "gpu": 1, "max_runtime_s": 60, "hyperparameters": LIMITS,
}


def test_a_row_becomes_a_preset_with_its_base_model_and_preprocessing():
    p = preset(TaskType.TEXT_CLASSIFICATION, "tiny", preprocessing={"max_length": 128})
    assert (p.task, p.name, p.base_model.revision) == (TaskType.TEXT_CLASSIFICATION, "tiny", REVISION)
    assert p.preprocessing == TextPreprocessing(max_length=128)


@pytest.mark.parametrize("over", [
    {"base_model": {"repo_id": "org/model", "revision": "main"}},
    {"max_runtime_s": settings.training.max_runtime_seconds + 1},
    {"learning_rate": 0},
    {"hyperparameters": {**LIMITS, "epochs": {"default": 600, "min": 1, "max": 500}}},
    {"surprise": 1},
])
def test_a_bad_preset_is_refused(over):
    with pytest.raises(ValidationError):
        PresetBody.model_validate({**BODY, **over})


def test_a_good_preset_is_accepted():
    assert PresetBody.model_validate(BODY).max_runtime_s == 60


def test_a_bundle_row_without_a_base_model_reads_back_with_its_default_values():
    p = preset(TaskType.TABULAR_CLASSIFICATION, "small", base_model_repo=None, base_model_revision=None, preprocessing={},
               hyperparameters={"target": "label", "n_estimators": 200})
    assert (p.base_model, p.preprocessing, p.hyperparameters) == (None, None, {"target": "label", "n_estimators": 200})
    assert preset().hyperparameters.epochs.default == LIMITS["epochs"]["default"]  # an admin row keeps its bounds


def _check(constraint):
    from tests.core.test_task_types import _latest_check

    return _latest_check(constraint)


def test_the_recipe_checks_mirror_the_enums():
    from app.core.task_catalog import Augmentation, LrSchedule

    assert _check("task_presets_lr_schedule_check") == {s.value for s in LrSchedule}
    assert _check("task_presets_augmentation_check") == {a.value for a in Augmentation}


def test_the_recipe_defaults_are_todays_behaviour():
    p = PresetBody.model_validate(BODY)
    assert (p.lr_schedule, p.warmup_fraction, p.augmentation, p.early_stopping_patience) == ("constant", 0, "none", 0)


@pytest.mark.parametrize("task, augmentation, ok", [
    (TaskType.IMAGE_CLASSIFICATION, "flip_crop", True), (TaskType.IMAGE_CLASSIFICATION, "flip", False),
    (TaskType.OBJECT_DETECTION, "flip", True), (TaskType.OBJECT_DETECTION, "flip_crop", False),
    (TaskType.TEXT_CLASSIFICATION, "flip", False), (TaskType.TEXT_CLASSIFICATION, "none", True),
])
def test_the_augmentation_must_be_one_the_tasks_trainer_has(task, augmentation, ok):
    body = PresetBody.model_validate({**BODY, "augmentation": augmentation})
    if ok:
        body.check_task(task)
    else:
        with pytest.raises(ValueError):
            body.check_task(task)


@pytest.mark.parametrize("over", [{"warmup_fraction": 1}, {"early_stopping_patience": -1}, {"lr_schedule": "linear"}, {"augmentation": "mixup"}])
def test_a_bad_recipe_is_refused(over):
    with pytest.raises(ValidationError):
        PresetBody.model_validate({**BODY, **over})


@pytest.mark.parametrize("over", [
    {"base_model": {"repo_id": "org/model", "revision": "main"}},
    {"max_runtime_s": settings.training.max_runtime_seconds + 1},
])
def test_a_bundles_preset_is_held_to_the_same_pin_and_ceiling(over):
    recipe = {k: v for k, v in BODY.items() if k != "hyperparameters"}
    assert ManifestPreset.model_validate({"name": "small", **recipe}).base_model.revision == REVISION
    with pytest.raises(ValidationError):
        ManifestPreset.model_validate({"name": "small", **recipe, **over})


def test_a_job_spec_declares_every_recipe_field_it_copies_the_way_the_recipe_does():
    """TrainPayload spells its fields out for the committed wire schema; this keeps them equal to the recipe's."""
    resources = set(Resources.model_fields)
    assert resources <= set(Recipe.model_fields)
    for name, field in Recipe.model_fields.items():
        if name in resources:
            continue
        mine = TrainPayload.model_fields[name]  # a recipe field the payload lacks fails here
        if name not in _SPEC_OWN:
            assert (mine.annotation, mine.metadata) == (field.annotation, field.metadata), name
