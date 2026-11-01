"""What datasets offers other contexts, and the ownership queries it registers with the gateway."""
import asyncio
import uuid

import pytest

from app.core.enums import TaskType
from app.core.problem import Problem
from app.modules.user.datasets import VersionSource, get_version_source, owns_dataset, owns_import, owns_version, readers
from tests.support import PROJECT, TENANT, FakeConn

DS, VER, IMP = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
TENANT_ID = uuid.UUID(TENANT)


def test_a_version_source_is_the_dataset_and_commit_of_a_live_dataset_of_the_project():
    conn = FakeConn(row={"dataset_id": DS, "lakefs_commit": "c0ffee"})
    assert asyncio.run(get_version_source(conn, PROJECT, VER)) == VersionSource(dataset_id=DS, lakefs_commit="c0ffee")
    assert conn.calls[0][1] == (VER, PROJECT)
    assert asyncio.run(get_version_source(FakeConn(row=None), PROJECT, VER)) is None  # another project's or tenant's looks missing


def test_the_task_of_a_live_dataset_is_its_projects():
    conn = FakeConn(vals={"FROM datasets": 1, "FROM projects": TaskType.OBJECT_DETECTION})
    assert asyncio.run(readers.dataset_task(conn, PROJECT, DS)) == TaskType.OBJECT_DETECTION


def test_a_project_without_a_task_type_is_told_apart_from_a_missing_dataset():
    assert asyncio.run(readers.dataset_task(FakeConn(vals={"FROM datasets": 1, "FROM projects": None}), PROJECT, DS)) is None
    with pytest.raises(Problem) as e:
        asyncio.run(readers.dataset_task(FakeConn(vals={"FROM datasets": None}), PROJECT, DS))
    assert e.value.status_code == 404


@pytest.mark.parametrize(("check", "ids", "expected"), [
    (owns_dataset, (DS,), (DS, TENANT_ID)),
    (owns_import, (IMP, DS), (IMP, DS, TENANT_ID)),
    (owns_version, (VER, DS), (VER, DS, TENANT_ID)),
])
def test_ownership_queries_match_the_tenant_as_well_as_rls(check, ids, expected):
    conn = FakeConn(val=1)
    assert asyncio.run(check(conn, TENANT_ID, *ids)) == 1
    sql, args = conn.calls[0]
    assert args == expected and "tenant_id" in sql
