#!/usr/bin/env python3
"""Fail on destructive SQL in the `-- migrate:up` part of dbmate migrations, and on files dbmate
would mis-handle (duplicate versions, missing markers).

    python3 scripts/lint-migrations.py core-api/migrations/*.sql

A destructive statement passes only with an opt-out comment inside it (or on the line before it):

    -- migration-lint: allow-destructive (why this is safe)
    ALTER TABLE t DROP COLUMN c;

Downs are not checked: undoing an up is destructive by nature, and the cluster never runs them.
"""
import re
import sys
from pathlib import Path

OPT_OUT = "migration-lint: allow-destructive"
RULES = [
    (re.compile(r"\bDROP\s+TABLE\b", re.I), "DROP TABLE"),
    (re.compile(r"\bTRUNCATE\b", re.I), "TRUNCATE"),
    # ALTER TABLE t DROP [COLUMN] c, but not DROP CONSTRAINT / DEFAULT / NOT NULL / IDENTITY / EXPRESSION.
    (re.compile(r"\bALTER\s+TABLE\b.*\bDROP\s+(?!CONSTRAINT\b|DEFAULT\b|NOT\b|IDENTITY\b|EXPRESSION\b)\w",
                re.I | re.S), "DROP COLUMN"),
    (re.compile(r"^(?!.*\bUSING\b).*\bALTER\s+COLUMN\s+\S+\s+(SET\s+DATA\s+)?TYPE\b", re.I | re.S),
     "ALTER COLUMN ... TYPE without USING"),
    (re.compile(r"^(?!.*\bWHERE\b).*\bDELETE\s+FROM\b", re.I | re.S), "DELETE without WHERE"),
]


def lint(path: Path) -> list[str]:
    text = path.read_text()
    if "-- migrate:up" not in text:
        return [f"{path.name}: no '-- migrate:up' marker"]
    up = text.split("-- migrate:up", 1)[1].split("-- migrate:down", 1)[0]
    errors = []
    # ponytail: splits on every ';', so a ';' inside a string literal cuts a statement in two.
    for chunk in up.split(";"):
        if OPT_OUT in chunk:
            continue
        sql = re.sub(r"--[^\n]*", "", chunk).strip()
        for rule, label in RULES:
            if rule.search(sql):
                errors.append(f"{path.name}: {label} without '-- {OPT_OUT}': {' '.join(sql.split())[:80]}")
    return errors


def main(paths: list[str]) -> int:
    files = [Path(p) for p in paths]
    errors = [e for f in files for e in lint(f)]
    seen: dict[str, str] = {}
    for f in files:
        version = re.match(r"\d+", f.name)
        if not version:
            errors.append(f"{f.name}: no numeric version prefix")
        elif version.group() in seen:
            errors.append(f"{f.name}: version {version.group()} already used by {seen[version.group()]}")
        else:
            seen[version.group()] = f.name
    for e in errors:
        print(e, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
