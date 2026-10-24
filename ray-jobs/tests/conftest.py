import json

import pytest


@pytest.fixture
def fake_parquet(monkeypatch):
    """Parquet swapped for JSON so the e2e tests need no pyarrow; the real round trips are in test_parquet.py."""
    monkeypatch.setattr("ray_jobs.runner.index_parquet", lambda rows: json.dumps(rows).encode())
    monkeypatch.setattr("ray_jobs.runner.table_parquet", lambda rows, table: json.dumps(rows).encode())
    monkeypatch.setattr("ray_jobs.merge.read_parquet", lambda data, max_bytes=None: json.loads(data))
    monkeypatch.setattr("ray_jobs.sources.huggingface.read_parquet", lambda data, max_bytes=None: json.loads(data))
