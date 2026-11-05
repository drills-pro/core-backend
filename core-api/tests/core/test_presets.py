"""task_presets: what an admin may write, and how a row becomes a TaskPreset."""
import pytest
from pydantic import ValidationError

from app.core.enums import TaskType
from app.core.presets import PresetBody
from app.core.settings import settings
from app.core.task_catalog import TextPreprocessing
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
