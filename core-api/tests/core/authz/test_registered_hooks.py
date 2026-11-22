"""The cross-context hook projects registers at import time fails closed when absent."""
import asyncio
import uuid

import pytest

from app.modules.user import projects  # noqa: F401  (registers the hooks)
from app.modules.user.tenant_members import hooks
from tests.support import FakeConn


def test_projects_registers_the_member_removed_hook():
    assert hooks._member_removed is projects.remove_member_bindings


def test_unregistered_member_hook_fails_closed(monkeypatch):
    monkeypatch.setattr(hooks, "_member_removed", None)
    with pytest.raises(RuntimeError):
        asyncio.run(hooks.member_removed(FakeConn(), uuid.uuid4(), "u"))


def test_removal_hook_deletes_bindings():
    conn = FakeConn()
    asyncio.run(hooks.member_removed(conn, uuid.uuid4(), "u"))
    assert "DELETE FROM project_members" in conn.calls[0][0]
