"""The staging store must not sign boto3's default checksum trailer: the staging endpoint is S3-compatible, not AWS."""
import os
import sys
import types

import pytest

from ray_core.store import CHECKSUM_ENV, CHECKSUM_WHEN_REQUIRED, S3Store, train_state_storage


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


def test_checkpoints_sit_under_a_top_level_train_state_prefix_per_run(monkeypatch, tmp_path):
    # Top level, not under the run prefix: a bucket lifecycle rule on train-state/ must not reach manifests or outputs.
    pytest.importorskip("pyarrow")
    monkeypatch.setenv("STAGING_ENDPOINT", f"file://{tmp_path}")
    _, path = train_state_storage("bucket", "trainings/run-1/")
    assert path == f"{tmp_path}/train-state/trainings/run-1/"  # Ray appends the job id under it
