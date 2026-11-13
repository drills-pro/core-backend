"""Every table the migrations leave with a tenant_id column has row-level security enabled and a tenant-isolation
policy, and every other table is a documented exception. Read from core-api/migrations/*.sql alone, so it fails in the
fast suite before a migration is ever applied; tests/db checks the same promise on a real Postgres.

The `-- migrate:up` half of each file is replayed in version order, so a table a later migration drops or recreates
counts as it ends up.
"""
import re
from pathlib import Path

import pytest

MIGRATIONS = sorted((Path(__file__).resolve().parents[2] / "migrations").glob("*.sql"))
TENANT_COLUMN = "tenant_id"
TENANT_GUC = "app.tenant_id"  # what TenantConn sets for each request and every policy reads
ISOLATION = re.compile(rf"{TENANT_COLUMN}\s*=\s*current_setting\(\s*'{re.escape(TENANT_GUC)}'")
# Tables with no tenant_id, so no RLS, on purpose. Each says why in the header of the migration that creates it:
# tenants is the root; principals, tenant_create_keys and rate_limits are used before a tenant exists; the rest is
# platform data that only the /v1/admin routes write.
UNSCOPED = frozenset({
    "tenants", "principals", "tenant_create_keys", "rate_limits", "job_packages", "plan_limits", "task_presets", "task_types",
    "job_package_audit", "task_bundle_settings",
})

_TOKEN = re.compile(r"--[^\n]*|/\*.*?\*/|'(?:[^']|'')*'|\$(\w*)\$.*?\$\1\$|;|[^;'$/-]+|.", re.S)
_CREATE_TABLE = re.compile(r"CREATE (?:UNLOGGED )?TABLE (?:IF NOT EXISTS )?(?:public\.)?(\w+) ?\((.*)\)", re.I)
_DROP_TABLE = re.compile(r"DROP TABLE (?:IF EXISTS )?(.+?)(?: CASCADE| RESTRICT)?$", re.I)
_ALTER_TABLE = re.compile(r"ALTER TABLE (?:IF EXISTS )?(?:ONLY )?(?:public\.)?(\w+) (.*)", re.I)
_CREATE_POLICY = re.compile(r"CREATE POLICY (\w+) ON (?:public\.)?(\w+)", re.I)
_DROP_POLICY = re.compile(r"DROP POLICY (?:IF EXISTS )?(\w+) ON (?:public\.)?(\w+)", re.I)
_LOOP = re.compile(r"FOREACH (\w+) IN ARRAY ARRAY\[([^\]]*)\] LOOP", re.I)  # the migrations' way to do many tables at once
_NOT_COLUMNS = {"CONSTRAINT", "PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "EXCLUDE", "LIKE"}


def statements(sql):
    """The statements of `sql`, comments dropped and whitespace collapsed; a ; inside a string or a $$ body ends nothing."""
    found, current = [], []
    for match in _TOKEN.finditer(sql):
        token = match.group()
        if token.startswith(("--", "/*")):
            continue
        if token == ";":
            found.append(" ".join("".join(current).split()))
            current = []
        else:
            current.append(token)
    found.append(" ".join("".join(current).split()))
    return [s for s in found if s]


def columns(body):
    """The column names in a CREATE TABLE body: its top-level comma-separated items that are not constraints."""
    items, depth, item = [], 0, ""
    for char in re.sub(r"'(?:[^']|'')*'", "''", body):
        depth += (char == "(") - (char == ")")
        if char == "," and depth == 0:
            items.append(item)
            item = ""
        else:
            item += char
    items.append(item)
    return {words[0].strip('"').lower() for i in items if (words := i.split()) and words[0].upper() not in _NOT_COLUMNS}


def apply(tables, stmt, source):
    if m := _CREATE_TABLE.match(stmt):
        tables[m[1]] = {"scoped": TENANT_COLUMN in columns(m[2]), "rls": False, "policies": {}, "file": source}
    elif m := _DROP_TABLE.match(stmt):
        for name in m[1].split(","):
            tables.pop(name.strip().removeprefix("public."), None)
    elif m := _ALTER_TABLE.match(stmt):
        table, action = tables[m[1]], m[2].upper()
        if "ENABLE ROW LEVEL SECURITY" in action:
            table["rls"] = True
        elif "DISABLE ROW LEVEL SECURITY" in action:
            table["rls"] = False
        if re.search(rf"ADD COLUMN (IF NOT EXISTS )?{TENANT_COLUMN.upper()}\b", action):
            table["scoped"] = True
    elif m := _CREATE_POLICY.match(stmt):
        tables[m[2]]["policies"][m[1]] = stmt
    elif m := _DROP_POLICY.match(stmt):
        tables[m[2]]["policies"].pop(m[1], None)
    elif stmt.upper().startswith("DO ") and (m := _LOOP.search(stmt)):
        var, array = m.groups()
        policy = re.search(rf"CREATE POLICY (\w+) ON %I (.*?)', {var}\)", stmt, re.I)
        for name in re.findall(r"'(\w+)'", array):
            if "ENABLE ROW LEVEL SECURITY" in stmt.upper():
                tables[name]["rls"] = True
            if policy:
                tables[name]["policies"][policy[1]] = policy[2].replace("''", "'")


def schema_after(sources):
    """{table: {scoped, rls, policies, file}} once every (file name, sql) in `sources` has run its up half."""
    tables = {}
    for name, sql in sources:
        for stmt in statements(sql.split("-- migrate:down")[0]):
            apply(tables, stmt, name)
    return tables


def problems(sources, unscoped=UNSCOPED):
    tables = schema_after(sources)
    found = []
    for name, table in sorted(tables.items()):
        where = f"{name} ({table['file']})"
        if table["scoped"]:
            if not table["rls"]:
                found.append(f"{where} has a {TENANT_COLUMN} column but no ENABLE ROW LEVEL SECURITY")
            if not any(ISOLATION.search(p) for p in table["policies"].values()):
                found.append(f"{where} has no policy limiting {TENANT_COLUMN} to current_setting('{TENANT_GUC}')")
        elif name not in unscoped:
            found.append(f"{where} has no {TENANT_COLUMN} column and is not a documented exception (UNSCOPED)")
    found += [f"{name} is in UNSCOPED but no migration leaves such a table" for name in sorted(set(unscoped) - tables.keys())]
    return found


def test_every_table_is_tenant_isolated_or_a_documented_exception():
    sources = [(p.name, p.read_text()) for p in MIGRATIONS]
    scoped = {name for name, table in schema_after(sources).items() if table["scoped"]}
    assert {"projects", "datasets", "tenant_members"} <= scoped  # the parser sees the schema; an empty one would pass below
    assert problems(sources) == []


A = "CREATE TABLE a (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants(id));"
RLS = "ALTER TABLE a ENABLE ROW LEVEL SECURITY;"
POLICY = "CREATE POLICY tenant_isolation ON a USING (tenant_id = current_setting('app.tenant_id', true)::uuid);"
LOOP = """DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['a', 'b']
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY tenant_isolation ON %I USING (tenant_id = current_setting(''app.tenant_id'', true)::uuid)', t);
  END LOOP;
END
$$;"""


@pytest.mark.parametrize("up, expected", [
    pytest.param(f"{A} {RLS} {POLICY}", None, id="isolated"),
    pytest.param(f"{A} {POLICY}", "ENABLE ROW LEVEL SECURITY", id="rls missing"),
    pytest.param(f"{A} {RLS}", "no policy", id="policy missing"),
    pytest.param(f"{A} {RLS} CREATE POLICY open ON a USING (true);", "no policy", id="policy that isolates nothing"),
    pytest.param(f"{A} {RLS} {POLICY} DROP POLICY tenant_isolation ON a;", "no policy", id="policy dropped"),
    pytest.param(f"{A} {RLS} {POLICY} ALTER TABLE a DISABLE ROW LEVEL SECURITY;", "ENABLE ROW LEVEL SECURITY", id="rls disabled"),
    pytest.param("CREATE TABLE a (id uuid);", "not a documented exception", id="table that is neither"),
    pytest.param("CREATE TABLE a (id uuid); ALTER TABLE a ADD COLUMN tenant_id uuid;", "ENABLE ROW LEVEL SECURITY", id="tenant_id added later"),
    pytest.param(f"{A} DROP TABLE a;", None, id="table dropped"),
    pytest.param(f"{A} CREATE TABLE b (tenant_id uuid); {LOOP}", None, id="DO loop over several tables"),
])
def test_the_check_catches_what_it_claims(up, expected):
    # The down half is bad on purpose: it must be ignored.
    sql = f"-- migrate:up\n{up}\n-- migrate:down\nCREATE TABLE ignored (tenant_id uuid);\n"
    found = problems([("0001_t.sql", sql)], unscoped=frozenset())
    assert (expected is None and not found) or (expected is not None and any(expected in f for f in found)), found


def test_a_stale_exception_is_reported():
    assert problems([("0001_t.sql", "-- migrate:up\n")], unscoped=frozenset({"gone"})) == [
        "gone is in UNSCOPED but no migration leaves such a table"
    ]
