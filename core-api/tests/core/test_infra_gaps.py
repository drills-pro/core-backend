"""rate-limit purge, TenantConn, secret_files error paths, auth_log, and the poller leader lock."""
import asyncio
import json
import logging

import pytest

from app import background
from app.core import auth_log, loki_log, rate_limit, secret_files, tenant_conn
from app.core.enums import AuthEvent, AuthResult
from app.core.settings import settings
from tests.support.fake_pool import RecordingConn, patch_pool


def test_purge_drops_only_windows_older_than_five_minutes(monkeypatch):
    conn = RecordingConn()
    patch_pool(monkeypatch, rate_limit, conn)
    monkeypatch.setattr(rate_limit.time, "time", lambda: 6000.0)  # minute 100
    asyncio.run(rate_limit.purge())
    [(method, sql, args)] = conn.calls
    assert method == "execute" and sql.startswith("DELETE FROM rate_limits") and args == (95,)


def test_tenant_conn_scopes_the_transaction_to_the_tenant_and_releases(monkeypatch):
    conn = RecordingConn()
    pool = patch_pool(monkeypatch, tenant_conn, conn)

    async def go():
        async with tenant_conn.TenantConn("t-1") as c:
            assert c is conn
            assert conn.calls[0][0] == "tx_enter"

    asyncio.run(go())
    assert conn.calls[1] == ("execute", "SELECT set_config('app.tenant_id', $1, true)", ("t-1",))
    assert conn.calls[-1][0] == "tx_exit" and pool.released == 1


def test_tenant_conn_rolls_back_and_releases_when_the_body_raises(monkeypatch):
    conn = RecordingConn()
    pool = patch_pool(monkeypatch, tenant_conn, conn)

    async def go():
        async with tenant_conn.TenantConn("t-1"):
            raise ValueError("boom")

    with pytest.raises(ValueError):
        asyncio.run(go())
    assert conn.calls[-1] == ("tx_exit", "", (ValueError,)) and pool.released == 1


@pytest.fixture
def lakefs_file(tmp_path, monkeypatch):
    path = tmp_path / "lakefs.json"
    monkeypatch.setattr(settings.secrets, "lakefs", path)
    secret_files._cache.clear()
    yield path
    secret_files._cache.clear()


def test_secret_file_that_is_not_json_is_a_secret_error_naming_the_secret(lakefs_file):
    lakefs_file.write_text("{nope")
    with pytest.raises(secret_files.SecretError, match="lakefs.*not valid JSON"):
        secret_files.get("lakefs")


def test_secret_file_missing_a_key_names_the_missing_key(lakefs_file):
    lakefs_file.write_text(json.dumps({"endpoint": "e", "access_key": "a"}))
    with pytest.raises(secret_files.SecretError, match=r"missing \['secret_key'\]"):
        secret_files.get("lakefs")


def test_problems_lists_each_unloadable_secret_and_honours_skip(lakefs_file, monkeypatch, tmp_path):
    for name in secret_files.REQUIRED:
        if name != "lakefs":
            monkeypatch.setattr(settings.secrets, name, tmp_path / f"{name}-absent.json")
    lakefs_file.write_text(json.dumps({"endpoint": "e", "access_key": "a", "secret_key": "s"}))
    everything = secret_files.problems()
    assert len(everything) == len(secret_files.REQUIRED) - 1 and not any("lakefs" in p for p in everything)
    skipped = secret_files.problems(skip=("db",))
    assert len(skipped) == len(everything) - 1 and not any("'db'" in p for p in skipped)


def test_auth_log_signed_in_and_token_rejected_carry_the_event_attributes(caplog):
    with caplog.at_level(logging.INFO, logger=auth_log.logger.name):
        auth_log.signed_in("sub-1", AuthEvent.SIGN_UP)
        auth_log.token_rejected(AuthResult.EXPIRED)
    signed, rejected = caplog.records
    assert signed.levelno == logging.INFO and getattr(signed, "auth.event") == "sign_up" and getattr(signed, "user.id") == "sub-1"
    assert rejected.levelno == logging.WARNING and getattr(rejected, "auth.result") == AuthResult.EXPIRED.value


class _FakeExporter:
    def __init__(self, **kw):
        assert kw["insecure"] is True


class _NoopProcessor:
    def on_emit(self, record): ...
    def shutdown(self): ...
    def force_flush(self, timeout_millis=30000):
        return True


def test_auth_log_setup_is_a_noop_in_test_and_ships_over_otlp_otherwise(monkeypatch):
    handlers = list(auth_log.logger.handlers)
    auth_log.setup()
    assert auth_log.logger.handlers == handlers  # env == test

    monkeypatch.setattr(settings, "env", "local")
    monkeypatch.setattr(loki_log, "OTLPLogExporter", _FakeExporter)
    monkeypatch.setattr(loki_log, "BatchLogRecordProcessor", lambda exporter: _NoopProcessor())
    before = auth_log.logger.propagate
    try:
        auth_log.setup()
        assert auth_log.logger.propagate is False
        assert len(auth_log.logger.handlers) == len(handlers) + 1
    finally:
        for h in auth_log.logger.handlers[len(handlers):]:
            auth_log.logger.removeHandler(h)
        auth_log.logger.propagate = before


def test_leader_lock_skips_the_poll_when_another_replica_holds_it(monkeypatch):
    conn = RecordingConn(fetchval_result=False)
    patch_pool(monkeypatch, background, conn)
    ran = []

    async def poll():
        ran.append(1)

    asyncio.run(background._run_as_leader("x", poll))
    assert ran == [] and [c[0] for c in conn.calls] == ["fetchval"]


def test_leader_lock_runs_the_poll_and_always_unlocks(monkeypatch):
    conn = RecordingConn(fetchval_result=True)
    patch_pool(monkeypatch, background, conn)

    async def poll():
        raise RuntimeError("poll failed")

    with pytest.raises(RuntimeError):
        asyncio.run(background._run_as_leader("x", poll))
    assert conn.calls[-1][1].startswith("SELECT pg_advisory_unlock")


def test_a_poller_that_raises_is_logged_and_the_loop_keeps_going(monkeypatch, caplog):
    sleeps = []

    async def poll():
        raise RuntimeError("down")

    async def sleep(n):
        sleeps.append(n)
        if len(sleeps) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(background.asyncio, "sleep", sleep)
    with caplog.at_level(logging.ERROR), pytest.raises(asyncio.CancelledError):
        asyncio.run(background._run_forever("training-reconcile", poll, 7))
    assert sleeps == [7, 7] and sum("poller training-reconcile failed" in r.message for r in caplog.records) == 2


def test_start_creates_one_named_task_per_poller_and_stop_cancels_them(monkeypatch):
    async def forever(*a):
        await asyncio.Event().wait()

    monkeypatch.setattr(background, "_run_forever", forever)

    async def go():
        tasks = background.start()
        names = {t.get_name() for t in tasks}
        await background.stop(tasks)
        return names, [t.cancelled() for t in tasks]

    names, cancelled = asyncio.run(go())
    assert names == {f"poller:{n}" for n, _, _ in background.POLLERS} and all(cancelled)
