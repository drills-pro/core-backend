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


def test_layers_merge_env_over_base():
    s = load("prod")
    assert s.pg.host == "pg-backend-rw.platform.svc.cluster.local"  # prod.yaml
    assert s.pg.pool_max == 20  # base.yaml, same section
    assert not s.auth.dev_headers


def test_env_var_overrides_yaml(monkeypatch):
    monkeypatch.setenv("CORE_PG__HOST", "elsewhere")
    assert load("local").pg.host == "elsewhere"


def test_local_override_file_wins_over_yaml_but_only_in_local(monkeypatch, tmp_path):
    for name in ("base.yaml", "local.yaml", "prod.yaml"):
        (tmp_path / name).write_text((CONFIG_DIR / name).read_text())
    (tmp_path / "local.override.yaml").write_text('zitadel:\n  project_id: "from-override"\n')
    monkeypatch.setattr("app.core.settings.CONFIG_DIR", tmp_path)
    assert load("local").zitadel.project_id == "from-override"
    assert load("prod").zitadel.project_id != "from-override"


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


def test_dataset_limits_and_data_plane_knobs_are_all_positive_and_in_base_yaml():
    base = yaml.safe_load((CONFIG_DIR / "base.yaml").read_text())
    for section in ("dataset_limits", "data_plane"):
        loaded = getattr(settings, section)
        assert set(base[section]) == set(type(loaded).model_fields), f"{section}: base.yaml and settings.py disagree"
    values = {**settings.dataset_limits.model_dump(), **{k: v for k, v in settings.data_plane.model_dump().items() if isinstance(v, int)}}
    assert values and all(v > 0 for v in values.values())


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_every_environment_names_the_staging_secret(env):
    assert load(env).secrets.staging.name.startswith("staging")
    assert load(env).secrets.ingest.name.startswith("ingest")  # a separate file: the job must not get the staging one


def test_runtime_env_defaults_for_uv_installs():
    cfg = load("test").compute.runtime_env
    assert cfg.uv_options == ["--extra-index-url", "https://download.pytorch.org/whl/cu128", "--index-strategy", "unsafe-best-match"]
    assert cfg.setup_timeout_seconds == 3600
