import os

os.environ["APP_ENV"] = "test"  # before anything imports app.core.settings

import pytest  # noqa: E402

from app.core.settings import settings  # noqa: E402


@pytest.fixture
def dev_headers(monkeypatch):
    monkeypatch.setattr(settings.auth, "dev_headers", True)
