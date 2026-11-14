"""The staging store must not sign boto3's default checksum trailer: the staging endpoint is S3-compatible, not AWS."""
import os
import sys
import types

from ray_core.store import CHECKSUM_ENV, CHECKSUM_WHEN_REQUIRED, S3Store


def _store(monkeypatch):
    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=lambda *a, **k: object()))
    return S3Store("bucket", "prefix/", "http://staging", "key", "secret")


def test_the_store_turns_checksums_off_unless_required(monkeypatch):
    for name in CHECKSUM_ENV:
        monkeypatch.setenv(name, "x")
        monkeypatch.delenv(name)  # recorded as unset, so the test leaves the environment as it found it
    _store(monkeypatch)
    assert {os.environ[n] for n in CHECKSUM_ENV} == {CHECKSUM_WHEN_REQUIRED}


def test_an_operators_own_setting_wins(monkeypatch):
    monkeypatch.setenv(CHECKSUM_ENV[0], "when_supported")
    monkeypatch.setenv(CHECKSUM_ENV[1], "when_supported")
    _store(monkeypatch)
    assert {os.environ[n] for n in CHECKSUM_ENV} == {"when_supported"}
