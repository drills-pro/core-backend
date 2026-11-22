"""The CHECK reader the drift tests share: a later migration wins, a dropped table takes its checks, quotes may hold parentheses."""
import pytest

from tests.support.check_mirror import assert_mirrors, assert_pattern_mirrors, check_pattern, check_values, checks_after

CREATE = """
CREATE TABLE things (
  id uuid PRIMARY KEY,
  state text NOT NULL CHECK (state IN ('a', 'b')),  -- CHECK (state IN ('comment'))
  slug text CONSTRAINT things_slug_format CHECK (slug ~ '^[a-z]+(-[a-z]+)*$' AND length(slug) < 9)
);
CREATE TABLE gone (kind text CHECK (kind IN ('x')));
-- migrate:down
"""
WIDEN = """
ALTER TABLE things DROP CONSTRAINT things_state_check;
ALTER TABLE things ADD CONSTRAINT things_state_check CHECK (state IN ('a', 'b', 'c'));
ALTER TABLE things ADD COLUMN mode text CHECK (mode IN ('on', 'off')), DROP CONSTRAINT IF EXISTS things_nope;
DROP TABLE gone CASCADE;
"""
CHECKS = checks_after(sql.split("-- migrate:down")[0] for sql in (CREATE, WIDEN))


def test_a_later_migration_that_re_adds_the_constraint_wins():
    assert check_values("things", "state", CHECKS) == {"a", "b", "c"}
    assert check_values("things", "mode", CHECKS) == {"on", "off"}  # an inline ADD COLUMN check gets Postgres' own name


def test_parentheses_inside_a_quoted_pattern_do_not_end_the_expression():
    assert CHECKS[("things", "things_slug_format")] == "slug ~ '^[a-z]+(-[a-z]+)*$' AND length(slug) < 9"  # an explicit name is kept
    assert check_pattern("things", "slug", {("things", "things_slug_check"): CHECKS[("things", "things_slug_format")]}) == "^[a-z]+(-[a-z]+)*$"


def test_a_dropped_table_takes_its_checks_with_it():
    with pytest.raises(AssertionError, match="gone_kind_check"):
        check_values("gone", "kind", CHECKS)


def test_a_mismatch_fails_the_assertion():
    with pytest.raises(AssertionError, match="database CHECK vs code"):
        assert_mirrors("model_versions", "promotion_status", ["pending"])


def test_the_real_migrations_are_readable():
    assert_mirrors("model_versions", "promotion_status", ["pending", "failed"])
    assert_pattern_mirrors("model_catalog", "revision", "^[0-9a-f]{40}$")
