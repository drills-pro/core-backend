"""Each response model built from a SQL function's rows has exactly that function's columns.

The function's newest definition across migrations/*.sql is the source of truth: a migration that adds, drops or
renames a column fails here until the model follows it. Fields the service adds on top (Zitadel's, say) are named
per model; `total` is the pager's count(*) OVER (), never part of a row. No database: the migrations are read as text.
"""
import re
from pathlib import Path

import pytest

from app.integrations.zitadel import OrgUser
from app.modules.admin.activity.schemas import ActivityItem, ActivityStats
from app.modules.admin.models.constants import ModelStatus
from app.modules.admin.models.schemas import AdminModel, AdminModelVersion, ModelStats
from app.modules.admin.tenants.schemas import TenantDataset, TenantMember, TenantProject

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"
_HEADER = re.compile(r"CREATE (?:OR REPLACE )?FUNCTION (\w+)\s*\(")
_RETURNS_TABLE = re.compile(r"\s*RETURNS TABLE\s*\(")
PAGER = {"total"}


def _balanced(sql: str, open_at: int) -> int:
    """The index just past the parenthesis that closes the one at `open_at`."""
    depth = 0
    for i in range(open_at, len(sql)):
        depth += {"(": 1, ")": -1}.get(sql[i], 0)
        if depth == 0:
            return i + 1
    raise ValueError("unbalanced parentheses")


def _columns(cols: str) -> list[str]:
    """Top-level column names of a RETURNS TABLE list: numeric(10, 2) and the like carry commas of their own."""
    names, depth, current = [], 0, ""
    for ch in cols + ",":
        depth += {"(": 1, ")": -1}.get(ch, 0)
        if ch == "," and depth == 0:
            if current.strip():
                names.append(current.split()[0])
            current = ""
        else:
            current += ch
    return names


def _definitions(sql: str):
    """(name, column names, body) of every RETURNS TABLE function in one SQL text."""
    for m in _HEADER.finditer(sql):
        after_args = _balanced(sql, m.end() - 1)
        table = _RETURNS_TABLE.match(sql, after_args)
        if table is None:
            continue
        end = _balanced(sql, table.end() - 1)
        body_start = sql.index("$$", end)
        yield m.group(1), _columns(sql[table.end() : end - 1]), sql[body_start : sql.index("$$", body_start + 2)]


SHAPES = {
    "admin_tenant_projects": (TenantProject, set()),
    "admin_tenant_members": (TenantMember, set(OrgUser.model_fields)),
    "admin_tenant_datasets": (TenantDataset, set()),
    "admin_activity": (ActivityItem, {"actor_name"}),
    "admin_activity_stats": (ActivityStats, set()),
    "admin_models": (AdminModel, set()),
    "admin_model_versions": (AdminModelVersion, set()),
    "admin_model_stats": (ModelStats, set()),
}


def _newest_definitions() -> dict[str, tuple[list[str], str]]:
    """{function: (its RETURNS TABLE column names, its body)}, the last migration to define it winning."""
    newest = {}
    for path in sorted(MIGRATIONS.glob("*.sql")):
        up = path.read_text(encoding="utf-8").split("-- migrate:down", 1)[0]
        for name, columns, body in _definitions(up):
            newest[name] = (columns, body)
    return newest


DEFINITIONS = _newest_definitions()


@pytest.mark.parametrize("function", SHAPES)
def test_the_model_has_the_functions_columns(function):
    model, added = SHAPES[function]
    columns, _ = DEFINITIONS[function]
    assert set(columns) - PAGER == set(model.model_fields) - added, model.__name__


def test_admin_models_derives_every_model_status():
    _, body = DEFINITIONS["admin_models"]
    assert all(f"'{status}'" in body for status in ModelStatus)


def test_the_parser_reads_columns_and_skips_type_arguments():
    sql = "CREATE OR REPLACE FUNCTION f(p int, q numeric(4, 1)) RETURNS TABLE(a uuid, b numeric(10, 2), c text[]) " \
          "LANGUAGE sql AS $$ SELECT 1 $$;"
    [(name, columns, body)] = _definitions(sql)
    assert (name, columns, body.strip("$ ")) == ("f", ["a", "b", "c"], "SELECT 1")
