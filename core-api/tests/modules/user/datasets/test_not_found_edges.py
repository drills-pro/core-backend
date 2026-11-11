"""404s and no-ops of the preview service, the dataset readers and import expiry."""
import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from starlette.exceptions import HTTPException

from app.core.authz import Action, AuthzContext, TenantRole
from app.core.enums import TaskType
from app.modules.user.datasets import preview_service, readers
from app.modules.user.datasets import reconcile as rec
from app.modules.user.datasets.constants import ImportStatus
from tests.support import PROJECT, TENANT, FakeConn, patch_tenant_conn

DS, VER, IMP = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
CTX = AuthzContext(TENANT, "u1", TenantRole.OWNER, PROJECT, frozenset(Action))


def _preview(monkeypatch, fn, *args, **vals):
    patch_tenant_conn(monkeypatch, FakeConn(vals={"FROM datasets": 1, **vals}), preview_service)
    with pytest.raises(HTTPException) as e:
        asyncio.run(fn(CTX, PROJECT, DS, *args, 10))
    return e.value.status_code


def test_a_project_without_a_task_type_has_nothing_to_preview(monkeypatch):
    assert _preview(monkeypatch, preview_service.version_preview, VER, **{"FROM projects": None}) == 404
    assert _preview(monkeypatch, preview_service.import_preview, IMP, **{"FROM projects": None}) == 404


def test_a_version_that_is_not_the_datasets_is_404(monkeypatch):
    assert _preview(monkeypatch, preview_service.version_preview, VER, **{"FROM projects": TaskType.IMAGE_CLASSIFICATION, "FROM dataset_versions": None}) == 404


def test_an_import_that_is_not_the_datasets_is_404_before_its_status_is_judged(monkeypatch):
    assert _preview(monkeypatch, preview_service.import_preview, IMP, **{"FROM projects": TaskType.IMAGE_CLASSIFICATION, "FROM dataset_imports": None}) == 404


@pytest.mark.parametrize("found", [1, None])
def test_in_project_says_whether_the_dataset_belongs_to_the_project(found):
    conn = FakeConn(val=found)
    assert asyncio.run(readers.in_project(conn, PROJECT, DS)) is (found is not None)
    assert conn.calls[0][1] == (DS, PROJECT)


def test_an_import_someone_else_moved_is_not_expired_or_audited(monkeypatch):
    async def lost(*a, **k):
        return None

    monkeypatch.setattr(rec.job_lifecycle, "advance", lost)
    conn = FakeConn()
    row = {"status": ImportStatus.AWAITING_REVIEW, "tenant_id": TENANT, "project_id": PROJECT, "id": IMP}
    assert asyncio.run(rec._expire(conn, row, datetime.now(UTC))) is False
    assert conn.executed == []  # no audit row, and the caller purges no staging
