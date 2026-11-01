"""SQL-text checks for the job-claims migration (there is no Postgres in the unit suite; migrations-check.sh runs it)."""
import re
from pathlib import Path

MIGRATIONS = Path(__file__).parents[2] / "migrations"
SQL = next(MIGRATIONS.glob("*_job_claims.sql")).read_text()
UP, DOWN = SQL.split("-- migrate:down")


def test_every_followed_table_gets_a_lease_column():
    for table in ("training_runs", "dataset_imports", "deployments"):
        assert re.search(rf"ALTER TABLE {table}\b.*?claimed_until timestamptz", UP, re.S), table


def test_the_deploy_clock_restarts_when_the_job_id_changes_and_is_not_security_definer():
    assert "deploying_since timestamptz NOT NULL DEFAULT now()" in UP
    assert "BEFORE UPDATE OF ray_job_id ON deployments" in UP and "OLD.ray_job_id IS DISTINCT FROM NEW.ray_job_id" in UP
    assert "SECURITY DEFINER" not in UP
    assert "GRANT EXECUTE ON FUNCTION deployments_restart_deploy_clock() TO core_app" in UP


def test_down_undoes_up():
    for part in ("DROP TRIGGER deployments_restart_deploy_clock", "DROP FUNCTION deployments_restart_deploy_clock", "DROP COLUMN deploying_since"):
        assert part in DOWN
    assert DOWN.count("claimed_until") == 3


def test_deployments_have_a_partial_index_for_the_claim():
    assert "CREATE INDEX deployments_deploying_idx ON deployments(tenant_id) WHERE status = 'deploying'" in UP
    assert "DROP INDEX deployments_deploying_idx" in DOWN
