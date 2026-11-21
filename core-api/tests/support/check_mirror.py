"""The CHECK constraints the migrations leave behind, for tests that keep a Python enum or regex in sync with the database.

`check_expression("projects", "task_type")` is the expression of the constraint `projects_task_type_check` as it stands once every
migration's `up` section has run in order: a later migration that drops and re-adds the constraint wins, a dropped table takes its
checks with it. `assert_mirrors` / `assert_pattern_mirrors` compare it with an enum or a regex. Only CREATE TABLE / ALTER TABLE
statements are read, so a CHECK named in a function body or a comment is ignored.
"""
import re
from collections.abc import Iterable
from enum import Enum
from functools import cache
from pathlib import Path

MIGRATIONS = Path(__file__).parents[2] / "migrations"
_DOWN_MARKER = "-- migrate:down"
_TABLE_STATEMENT = re.compile(r"\s*(?:ALTER|CREATE)\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:ONLY\s+)?(\w+)", re.I)
_DROP_TABLE = re.compile(r"\s*DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?([\w\s,]+?)(?:\s+CASCADE)?\s*$", re.I)
_DROP_CONSTRAINT = re.compile(r"DROP\s+CONSTRAINT\s+(?:IF\s+EXISTS\s+)?(\w+)", re.I)
_CHECK = re.compile(r"(?:CONSTRAINT\s+(\w+)\s+)?CHECK\s*\(", re.I)
_FIRST_COLUMN = re.compile(r"\(*\s*(\w+)")

Checks = dict[tuple[str, str], str]  # (table, constraint name) -> expression


def _statements(sql: str) -> list[str]:
    return [s for s in re.sub(r"--[^\n]*", "", sql).split(";") if s.strip()]


def _balanced(text: str, start: int) -> int:
    """The index just past the parenthesis that closes the one opened at text[start - 1]; quoted text is skipped."""
    depth, i, quoted = 1, start, False
    while depth:
        c = text[i]
        if c == "'":
            quoted = not quoted
        elif not quoted:
            depth += (c == "(") - (c == ")")
        i += 1
    return i


def checks_after(up_sections: Iterable[str]) -> Checks:
    """The CHECK constraints that exist once these `up` sections have run, in order."""
    checks: Checks = {}
    for sql in up_sections:
        for statement in _statements(sql):
            if dropped := _DROP_TABLE.match(statement):
                tables = {t.strip() for t in dropped.group(1).split(",")}
                checks = {key: expr for key, expr in checks.items() if key[0] not in tables}
                continue
            if not (head := _TABLE_STATEMENT.match(statement)):
                continue
            table = head.group(1)
            for name in _DROP_CONSTRAINT.findall(statement):  # one ALTER drops before it adds
                checks.pop((table, name), None)
            for found in _CHECK.finditer(statement):
                expression = statement[found.end():_balanced(statement, found.end()) - 1].strip()
                name = found.group(1) or f"{table}_{_FIRST_COLUMN.match(expression).group(1)}_check"  # Postgres' own name for an inline CHECK
                checks[(table, name)] = expression
    return checks


@cache
def _current() -> Checks:
    return checks_after(p.read_text().split(_DOWN_MARKER)[0] for p in sorted(MIGRATIONS.glob("*.sql")))


def check_expression(table: str, column: str, checks: Checks | None = None) -> str:
    """The expression of the constraint `<table>_<column>_check`."""
    found = (_current() if checks is None else checks).get((table, f"{table}_{column}_check"))
    assert found is not None, f"no CHECK {table}_{column}_check in the migrations"
    return found


def check_values(table: str, column: str, checks: Checks | None = None) -> set[str]:
    """The values of `column IN (...)` in that expression."""
    expression = check_expression(table, column, checks)
    in_list = re.search(rf"\b{column}\s+IN\s*\(([^)]*)\)", expression, re.I)
    assert in_list, f"{table}_{column}_check has no `{column} IN (...)`: {expression}"
    return set(re.findall(r"'([^']+)'", in_list.group(1)))


def check_pattern(table: str, column: str, checks: Checks | None = None) -> str:
    """The regex of `column ~ '...'` in that expression."""
    expression = check_expression(table, column, checks)
    pattern = re.search(rf"\b{column}\s*~\s*'((?:[^']|'')*)'", expression)
    assert pattern, f"{table}_{column}_check has no `{column} ~ '...'`: {expression}"
    return pattern.group(1).replace("''", "'")


def assert_mirrors(table: str, column: str, values: type[Enum] | Iterable[str]) -> None:
    """`table.column` accepts exactly the members of an enum (their values) or the given strings."""
    expected = {m.value for m in values} if isinstance(values, type) and issubclass(values, Enum) else set(values)
    assert check_values(table, column) == expected, f"{table}.{column}: database CHECK vs code"


def assert_pattern_mirrors(table: str, column: str, pattern: str) -> None:
    """`table.column ~ '...'` is the same regex the code validates with."""
    assert check_pattern(table, column) == pattern, f"{table}.{column}: database CHECK vs code"
