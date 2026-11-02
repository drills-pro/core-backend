"""core/task_types: what the platform knows per task type, read from the task_types table."""
import asyncio

import pytest

from app.core import task_types
from app.core.enums import TaskType
from app.core.problem import Problem
from app.core.task_catalog import DataKind
from tests.support import FakeConn
from tests.support.task_types import task_row


def info(task, **over):
    return asyncio.run(task_types.get(FakeConn(rows={"FROM task_types": task_row(task, **over)}), task))


def test_image_tasks_are_trainable_and_tabular_ones_are_not_yet():
    assert info(TaskType.IMAGE_CLASSIFICATION).trainable and info(TaskType.OBJECT_DETECTION).trainable
    assert not info(TaskType.TABULAR_REGRESSION).trainable
    assert asyncio.run(task_types.get(FakeConn(), TaskType.TEXT_GENERATION)) is None  # no row at all


def test_the_predicates_are_the_row():
    assert info(TaskType.TABULAR_CLASSIFICATION).is_tabular and not info(TaskType.OBJECT_DETECTION).is_tabular
    assert info(TaskType.TEXT_CLASSIFICATION).is_table and info(TaskType.TABULAR_REGRESSION).is_table
    assert not info(TaskType.OBJECT_DETECTION).is_table
    assert info(TaskType.IMAGE_CLASSIFICATION).servable and not info(TaskType.TABULAR_REGRESSION).servable


@pytest.mark.parametrize("row", [None, task_row(enabled=False)])
def test_a_task_without_a_row_or_switched_off_is_a_422(row):
    with pytest.raises(Problem) as e:
        asyncio.run(task_types.require_supported(FakeConn(rows={"FROM task_types": row}), TaskType.IMAGE_CLASSIFICATION))
    assert (e.value.status_code, e.value.code) == (422, "task_type_not_supported")


def test_a_supported_task_is_returned_with_its_data_kind():
    assert asyncio.run(task_types.require_supported(FakeConn(), TaskType.OBJECT_DETECTION)).data_kind is DataKind.IMAGES
