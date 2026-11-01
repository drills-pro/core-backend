"""What projects offers other contexts."""
import asyncio

from app.core.enums import TaskType
from app.modules.user.projects import get_settings, get_task_type, owns_project
from tests.support import PROJECT, TENANT, FakeConn
import uuid


def test_task_type_is_a_task_type_or_none():
    assert asyncio.run(get_task_type(FakeConn(val="image-classification"), PROJECT)) is TaskType.IMAGE_CLASSIFICATION
    assert asyncio.run(get_task_type(FakeConn(val=None), PROJECT)) is None


def test_settings_default_to_an_empty_bag():
    assert asyncio.run(get_settings(FakeConn(val={"deploy_strategy": "main"}), PROJECT)) == {"deploy_strategy": "main"}
    assert asyncio.run(get_settings(FakeConn(val=None), PROJECT)) == {}


def test_ownership_matches_the_tenant():
    conn = FakeConn(val=1)
    assert asyncio.run(owns_project(conn, uuid.UUID(TENANT), PROJECT)) == 1
    assert conn.calls[0][1] == (PROJECT, uuid.UUID(TENANT))
