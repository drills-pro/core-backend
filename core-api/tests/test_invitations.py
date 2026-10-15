import asyncio
import hashlib
import uuid

from app.modules.user.projects import members


class FakeConn:
    def __init__(self):
        self.calls = []

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        return {"id": "i1", "email": "a@b.co", "role": "project_viewer", "expires_at": "x"}

    async def execute(self, sql, *args):
        self.calls.append((sql, args))


def _patch(monkeypatch, conn):
    class TC:
        def __init__(self, tenant_id):
            pass

        async def __aenter__(self):
            return conn

        async def __aexit__(self, *e):
            return False

    async def noop(*a, **k):
        pass

    monkeypatch.setattr(members, "TenantConn", TC)
    monkeypatch.setattr(members.audit, "record", noop)


def test_create_invitation_stores_only_hash_and_returns_plaintext_once(monkeypatch):
    conn = FakeConn()
    _patch(monkeypatch, conn)
    out = asyncio.run(
        members.create_invitation(uuid.uuid4(), "a@b.co", "project_viewer", tenant_id="t", actor="u")
    )
    sql, args = conn.calls[0]
    token = out["token"]
    assert "token_hash" in sql
    assert hashlib.sha256(token.encode()).hexdigest() in args
    assert token not in args


def test_accept_invitation_looks_up_by_hash_and_clears_email(monkeypatch):
    conn = FakeConn()

    async def fetchrow(sql, *args):
        conn.calls.append((sql, args))
        return {"id": "i1", "role": "project_viewer", "expires_at": members.datetime.max.replace(tzinfo=members.timezone.utc), "accepted_at": None}

    conn.fetchrow = fetchrow
    _patch(monkeypatch, conn)
    asyncio.run(members.accept_invitation(uuid.uuid4(), "tok", tenant_id="t", actor="u"))
    lookup_sql, lookup_args = conn.calls[0]
    assert "token_hash" in lookup_sql and hashlib.sha256(b"tok").hexdigest() in lookup_args
    assert any("email = NULL" in sql for sql, _ in conn.calls)
