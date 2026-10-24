"""SQL-text checks for the dataset migrations (there is no Postgres in the unit suite; migrations-check.sh runs them)."""
import re
from pathlib import Path

MIGRATIONS = Path(__file__).parents[2] / "migrations"


def _up(glob: str) -> str:
    text = next(MIGRATIONS.glob(glob)).read_text()
    return text.split("-- migrate:down")[0]


def _function(sql: str, name: str) -> str:
    return re.search(rf"FUNCTION {name}\(.*?\n\$\$;", sql, re.S).group(0)


def test_purge_function_is_pinned_to_the_callers_tenant():
    body = _function(_up("*_dataset_sweeps.sql"), "purge_dataset_rows")
    assert "tenant_id = current_setting('app.tenant_id', true)::uuid" in body


def test_admin_views_leave_out_soft_deleted_datasets():
    sql = _up("*_admin_datasets_soft_delete.sql")
    for name in ("admin_tenant_projects", "admin_tenant_datasets", "admin_tenant_detail_stats"):
        assert "d.deleted_at IS NULL" in _function(sql, name), name
