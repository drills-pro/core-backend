"""Project sharing: the enum mirrors the DB CHECK, and the two writers move the level the way the decision says."""
import asyncio
import re
import uuid
from pathlib import Path

from app.core.authz import Action, AuthzContext, TenantRole, Visibility
from app.modules.user.projects import service
from app.modules.user.projects.constants import Sharing
from tests.support import FakeConn, patch_tenant_conn

MIGRATIONS = Path(__file__).parents[4] / "migrations"
CTX = AuthzContext("t", "u", TenantRole.MEMBER, uuid.uuid4(), frozenset(Action))
ROW = {"id": CTX.project_id, "sharing": Sharing.GALLERY, "visibility": Visibility.INTERNAL, "default_access": "read"}


def test_the_sharing_check_mirrors_the_enum():
    found = None
    for path in sorted(MIGRATIONS.glob("*.sql")):
        up = path.read_text().split("-- migrate:down")[0]
        for clause in re.findall(r"projects_sharing_check\s+CHECK \(sharing IN \((.*?)\)\)", up, re.S):
            found = set(re.findall(r"'([^']+)'", clause))
    assert found == {s.value for s in Sharing}


def _run(monkeypatch, fn, *args):
    conn = FakeConn(row=ROW)
    patch_tenant_conn(monkeypatch, conn, service)

    async def _noop(*a, **k):
        pass

    monkeypatch.setattr(service.audit, "record_for", _noop)
    asyncio.run(fn(CTX, CTX.project_id, *args))
    return conn.calls[0]


def test_a_gallery_level_makes_the_project_internal_and_private_only_unpublishes(monkeypatch):
    sql, args = _run(monkeypatch, service.update_sharing, Sharing.GALLERY_DOWNLOADS)
    assert args[1] == Sharing.GALLERY_DOWNLOADS and args[4] == Visibility.INTERNAL
    assert "sharing = $2" in sql and "ELSE $5 END" in sql


def test_making_a_project_private_also_resets_its_sharing(monkeypatch):
    sql, args = _run(monkeypatch, service.update_visibility, Visibility.PRIVATE, None)
    assert Visibility.PRIVATE in args and Sharing.PRIVATE in args and "sharing = CASE" in sql
