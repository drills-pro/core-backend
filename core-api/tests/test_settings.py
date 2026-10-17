import json
import os
import re

import pytest
import yaml

from app.core import secret_files
from app.core.settings import CONFIG_DIR, ENVIRONMENTS, load, settings


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_every_environment_loads(env):
    assert load(env).env == env


def test_app_env_is_required(monkeypatch):
    monkeypatch.delenv("APP_ENV")
    with pytest.raises(RuntimeError, match="APP_ENV"):
        load()


def test_env_file_overrides_base_and_deep_merges():
    s = load("prod")
    assert s.pg.host == "pg-backend-rw.platform.svc.cluster.local"  # prod.yaml
    assert s.pg.pool_max == 20  # base.yaml, same section
    assert not s.auth.dev_headers


def test_env_var_overrides_yaml(monkeypatch):
    monkeypatch.setenv("CORE_PG__HOST", "elsewhere")
    assert load("local").pg.host == "elsewhere"


def test_prod_refuses_dev_headers(monkeypatch):
    monkeypatch.setenv("CORE_AUTH__DEV_HEADERS", "true")
    with pytest.raises(ValueError, match="dev_headers"):
        load("prod")


def test_prod_refuses_relative_secret_paths(monkeypatch):
    monkeypatch.setenv("CORE_SECRETS__LAKEFS", "relative/minio.json")
    with pytest.raises(ValueError, match="absolute"):
        load("prod")


def test_relative_secret_paths_resolve_against_core_api_not_cwd(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    path = load("local").secrets.lakefs
    assert path.is_absolute() and path.name == "lakefs.json" and tmp_path not in path.parents


_SECRETISH = re.compile(r"password|secret|token|private|api_?key|credential", re.I)


def _leaves(node, trail=()):
    for key, value in node.items():
        if isinstance(value, dict):
            yield from _leaves(value, trail + (key,))
        else:
            yield trail + (key,), value


@pytest.mark.parametrize("path", sorted(CONFIG_DIR.glob("*.yaml")), ids=lambda p: p.name)
def test_committed_config_holds_no_secret_values(path):
    for trail, value in _leaves(yaml.safe_load(path.read_text())):
        if trail[0] == "secrets" or trail[-1].endswith("_file"):
            assert isinstance(value, str) and "/" in value, f"{'.'.join(trail)} must be a file path"
        else:
            assert not _SECRETISH.search(trail[-1]), f"{'.'.join(trail)} looks like a secret; reference it by file"


@pytest.fixture
def lakefs_file(monkeypatch, tmp_path):
    path = tmp_path / "lakefs.json"
    monkeypatch.setattr(settings.secrets, "lakefs", path)
    secret_files._cache.clear()
    return path


def test_secret_reread_when_file_changes(lakefs_file):
    lakefs_file.write_text(json.dumps({"endpoint": "e", "access_key": "a", "secret_key": "1"}))
    assert secret_files.get("lakefs")["secret_key"] == "1"
    lakefs_file.write_text(json.dumps({"endpoint": "e", "access_key": "a", "secret_key": "2"}))
    os.utime(lakefs_file, ns=(1, lakefs_file.stat().st_mtime_ns + 1_000_000))  # rotation bumps mtime
    assert secret_files.get("lakefs")["secret_key"] == "2"


def test_secret_errors_name_the_file(lakefs_file):
    with pytest.raises(secret_files.SecretError, match="no file at"):
        secret_files.get("lakefs")
    lakefs_file.write_text(json.dumps({"endpoint": "e"}))
    with pytest.raises(secret_files.SecretError, match="secret_key"):
        secret_files.get("lakefs")


def test_prod_has_the_zitadel_project_id():
    assert load("prod").zitadel.project_id.isdigit()
