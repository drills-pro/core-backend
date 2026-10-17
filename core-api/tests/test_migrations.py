"""The repo's migrations pass scripts/lint-migrations.py, and the lint catches what it claims to."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("lint_migrations", ROOT / "scripts/lint-migrations.py")
lint_migrations = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lint_migrations)


def test_repo_migrations_pass():
    files = sorted(str(p) for p in (ROOT / "core-api/migrations").glob("*.sql"))
    assert lint_migrations.main(files) == 0


def test_lint_flags_destructive_sql(tmp_path):
    bad = [
        "DROP TABLE t;",
        "TRUNCATE t;",
        "ALTER TABLE t DROP COLUMN c;",
        "ALTER TABLE t DROP IF EXISTS c;",
        "ALTER TABLE t ALTER COLUMN c TYPE int;",
        "DELETE FROM t;",
    ]
    for i, sql in enumerate(bad):
        f = tmp_path / f"{i:04d}_bad.sql"
        f.write_text(f"-- migrate:up\n{sql}\n-- migrate:down\n")
        assert lint_migrations.lint(f), sql

    ok = tmp_path / "0100_ok.sql"
    ok.write_text(
        "-- migrate:up\n"
        "ALTER TABLE t ALTER COLUMN c DROP NOT NULL;\n"
        "ALTER TABLE t DROP CONSTRAINT IF EXISTS k;\n"
        "ALTER TABLE t ALTER COLUMN c TYPE int USING c::int;\n"
        "DELETE FROM t WHERE id = 1;\n"
        "-- migration-lint: allow-destructive (test)\n"
        "DROP TABLE t;\n"
        "-- migrate:down\n"
        "DROP TABLE t;\n"
    )
    assert lint_migrations.lint(ok) == []


def test_lint_flags_duplicate_versions_and_missing_marker(tmp_path):
    a, b = tmp_path / "0001_a.sql", tmp_path / "0001_b.sql"
    a.write_text("-- migrate:up\n")
    b.write_text("SELECT 1;\n")
    assert lint_migrations.main([str(a), str(b)]) == 1
