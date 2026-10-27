"""Preset table (plan T5/T6): one hardcoded base model per preset and task, plus default hyperparameters.

ponytail: a Python table; becomes a Postgres table when presets must change without a deploy.
"""
from dataclasses import dataclass

from ray_training.constants import Preset, Task


@dataclass(frozen=True)
class PresetSpec:
    base_model: str  # Hugging Face repo id; the job spec's base_model.revision pins it
    learning_rate: float


PRESETS: dict[Task, dict[Preset, PresetSpec]] = {
    Task.IMAGE_CLASSIFICATION: {
        Preset.SMALL: PresetSpec("facebook/convnext-tiny-224", 1e-4),
        Preset.MEDIUM: PresetSpec("facebook/convnext-small-224", 1e-4),
        Preset.LARGE: PresetSpec("facebook/convnext-base-224", 5e-5),
    },
    Task.OBJECT_DETECTION: {
        Preset.SMALL: PresetSpec("PekingU/rtdetr_v2_r18vd", 1e-4),
        Preset.MEDIUM: PresetSpec("PekingU/rtdetr_v2_r50vd", 1e-4),
        Preset.LARGE: PresetSpec("PekingU/rtdetr_v2_r101vd", 5e-5),
    },
}
