"""task_catalog: what the platform knows per task type, in one table."""
import pytest

from app.core.enums import TaskType, TrainPreset
from app.core.problem import Problem
from app.core import task_catalog
from app.core.task_catalog import DataKind


def test_image_tasks_are_trainable_and_tabular_ones_are_not_yet():
    assert task_catalog.trainable(TaskType.IMAGE_CLASSIFICATION) and task_catalog.trainable(TaskType.OBJECT_DETECTION)
    assert not task_catalog.trainable(TaskType.TABULAR_REGRESSION)
    assert not task_catalog.trainable(TaskType.TEXT_GENERATION)  # not in the catalog at all


def test_the_predicates_are_the_catalog():
    assert task_catalog.supported(TaskType.TABULAR_REGRESSION) and not task_catalog.supported(TaskType.TEXT_GENERATION)
    assert task_catalog.is_tabular(TaskType.TABULAR_CLASSIFICATION) and not task_catalog.is_tabular(TaskType.OBJECT_DETECTION)
    assert task_catalog.servable(TaskType.IMAGE_CLASSIFICATION) and task_catalog.servable(TaskType.OBJECT_DETECTION)
    assert not task_catalog.servable(TaskType.TEXT_GENERATION)


def test_an_unsupported_task_is_a_422():
    task_catalog.require_supported_task_type(TaskType.IMAGE_CLASSIFICATION)
    with pytest.raises(Problem) as e:
        task_catalog.require_supported_task_type(TaskType.TEXT_GENERATION)
    assert (e.value.status_code, e.value.code) == (422, "task_type_not_supported")


def test_data_kind():
    assert task_catalog.info(TaskType.OBJECT_DETECTION).data_kind is DataKind.IMAGES
    assert task_catalog.info(TaskType.TABULAR_CLASSIFICATION).data_kind is DataKind.TABULAR


def test_base_model_is_looked_up_by_task_and_preset():
    assert task_catalog.base_model(TaskType.IMAGE_CLASSIFICATION, TrainPreset.SMALL).repo_id == "facebook/convnext-tiny-224"
    assert task_catalog.base_model(TaskType.OBJECT_DETECTION, TrainPreset.LARGE).repo_id == "PekingU/rtdetr_v2_r50vd"


def test_an_untrainable_task_has_no_base_model():
    with pytest.raises(KeyError):
        task_catalog.base_model(TaskType.TABULAR_REGRESSION, TrainPreset.SMALL)


def test_a_preset_carries_learning_rate_and_preprocessing_and_defaults_to_imagenet():
    spec = task_catalog.preset_spec(TaskType.OBJECT_DETECTION, TrainPreset.MEDIUM)
    assert (spec.base_model.repo_id, spec.learning_rate) == ("PekingU/rtdetr_v2_r34vd", 1e-4)
    assert spec.preprocessing == task_catalog.IMAGENET_PREPROCESSING
