"""plan_limits: the table seeds today's YAML values, mirrors the models, and lays a tenant's plan over the YAML limits."""
import asyncio
import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core import plan_limits
from app.core.enums import PlanTier
from app.core.plan_limits import PlanLimitsBody
from app.core.problem import Problem
from app.core.settings import settings
from tests.support import FakeConn

MIGRATIONS = Path(__file__).parents[2] / "migrations"
SQL = next(MIGRATIONS.glob("*_plan_limits.sql")).read_text()
PREDICT_SQL = next(MIGRATIONS.glob("*_plan_limits_predict.sql")).read_text()
DEPLOYMENTS_SQL = next(MIGRATIONS.glob("*_deployments_quota_idempotency.sql")).read_text().split("-- migrate:down")[0]
INTERNAL_SQL = next(MIGRATIONS.glob("*_internal_plan_tier.sql")).read_text().split("-- migrate:down")[0]


def groups(**over) -> dict:
    """A plan's limits as the YAML has them today, grouped as the table groups them; `over` replaces whole groups."""
    values = {**settings.dataset_limits.model_dump(), "max_concurrent_training_runs": settings.ray.max_concurrent_training_runs,
              "max_body_bytes": settings.serving.max_body_bytes, "max_inflight_per_deployment": settings.serving.max_inflight_per_deployment,
              "max_active_deployments": settings.serving.max_active_deployments_per_tenant}
    return {name: {k: values[k] for k in model.model_fields}
            for name, model in ((n, f.annotation) for n, f in PlanLimitsBody.model_fields.items())} | over


def row(**over) -> dict:
    return {"plan_tier": PlanTier.PRO, "updated_by": "ops", "updated_at": datetime.now(UTC), **groups(), **over}


def test_the_columns_and_the_tier_check_mirror_the_models_and_the_enum():
    columns = re.findall(r"^  (\w+) jsonb NOT NULL CHECK \(jsonb_typeof\(\1\) = 'object'\)", SQL, re.M)
    added = re.findall(r"ADD COLUMN (\w+) jsonb NOT NULL", PREDICT_SQL)
    added += re.findall(r"ADD COLUMN (\w+) jsonb NOT NULL", DEPLOYMENTS_SQL)
    assert columns + added == list(PlanLimitsBody.model_fields)
    for table in ("tenants", "plan_limits"):  # the latest CHECK on each table, which the internal tier migration rewrote
        tiers = re.search(rf"ADD CONSTRAINT {table}_plan_tier_check\s+CHECK \(plan_tier IN \(([^)]*)\)", INTERNAL_SQL).group(1)
        assert set(re.findall(r"'([^']+)'", tiers)) == {t.value for t in PlanTier}


def test_every_tier_is_seeded_with_the_yaml_values():
    seeded = {name: json.loads(text) for name, text in re.findall(r"^\s+'(\{.*\})'::jsonb,?$", SQL, re.M) and zip(
        PlanLimitsBody.model_fields, re.findall(r"^\s+'(\{.*\})'::jsonb,?$", SQL, re.M))}
    assert seeded == {k: v for k, v in groups().items() if k not in plan_limits.NOT_DATASET_GROUPS}
    predict_default = json.loads(re.search(r"DEFAULT '(\{.*\})'::jsonb", PREDICT_SQL).group(1))
    assert predict_default == groups()["predict"]  # the column default is the YAML fallback
    deployments_default = json.loads(re.search(r"DEFAULT '(\{.*\})'::jsonb", DEPLOYMENTS_SQL).group(1))
    assert deployments_default == groups()["deployments"]  # the column default is the YAML fallback
    updated = re.findall(r"WHERE plan_tier (?:= '(\w+)'|IN \(([^)]*)\))", DEPLOYMENTS_SQL)
    tiers = {t for single, many in updated for t in ([single] if single else re.findall(r"'(\w+)'", many))}
    assert tiers | {PlanTier.FREE} == {t.value for t in PlanTier}  # every other tier, `internal` included, is seeded explicitly
    seeded_tiers = set(re.findall(r"ARRAY\[([^\]]*)\]", SQL)[0].replace("'", "").split(", "))
    assert seeded_tiers | {PlanTier.INTERNAL} == {t.value for t in PlanTier}
    assert "SELECT 'internal'" in INTERNAL_SQL  # seeded later, as a copy of enterprise


def test_a_tenant_without_a_row_gets_the_yaml_limits():
    conn = FakeConn(row=None)
    assert asyncio.run(plan_limits.dataset_limits(conn, "t")) == settings.dataset_limits
    assert asyncio.run(plan_limits.max_training_runs(conn, "t")) == settings.ray.max_concurrent_training_runs


def test_a_plan_row_is_laid_over_the_yaml_limits_and_leaves_the_rest():
    g = groups()
    conn = FakeConn(rows={"FROM plan_limits": row(
        archives={**g["archives"], "upload_max_bytes": 5},
        concurrency={"max_active_imports_per_tenant": 1, "max_concurrent_training_runs": 2})})
    limits = asyncio.run(plan_limits.dataset_limits(conn, "t"))
    assert limits.upload_max_bytes == 5 and limits.max_active_imports_per_tenant == 1
    assert limits.max_files == settings.dataset_limits.max_files
    assert limits.job_timeout_seconds == settings.dataset_limits.job_timeout_seconds
    assert limits.ray_job_memory_bytes == settings.dataset_limits.ray_job_memory_bytes
    assert asyncio.run(plan_limits.max_training_runs(conn, "t")) == 2


def test_the_plan_never_reaches_a_limit_it_does_not_own():
    owned = set(PlanLimitsBody(**groups()).dataset_overrides())
    assert owned < set(type(settings.dataset_limits).model_fields) and not {"job_timeout_seconds", "ray_job_cpus", "ray_job_memory_bytes"} & owned


def test_concurrency_above_the_yaml_ceiling_is_refused():
    over = groups(concurrency={"max_active_imports_per_tenant": 1, "max_concurrent_training_runs": settings.ray.max_concurrent_training_runs + 1})
    with pytest.raises(Problem) as e:
        asyncio.run(plan_limits.put(FakeConn(), PlanTier.PRO, PlanLimitsBody(**over), "ops"))
    assert e.value.code == plan_limits.LIMIT_ABOVE_CEILING


def test_a_tenant_without_a_row_gets_the_yaml_predict_limits_and_a_plan_row_replaces_them():
    assert asyncio.run(plan_limits.predict_limits(FakeConn(row=None), "t")) == plan_limits.Predict(**groups()["predict"])
    conn = FakeConn(rows={"FROM plan_limits": row(predict={"max_body_bytes": 7, "max_inflight_per_deployment": 2})})
    assert asyncio.run(plan_limits.predict_limits(conn, "t")).max_body_bytes == 7


def test_a_body_cap_above_the_yaml_ceiling_is_refused():
    over = groups(predict={"max_body_bytes": settings.serving.max_body_bytes + 1, "max_inflight_per_deployment": 1})
    with pytest.raises(Problem) as e:
        asyncio.run(plan_limits.put(FakeConn(), PlanTier.PRO, PlanLimitsBody(**over), "ops"))
    assert e.value.code == plan_limits.LIMIT_ABOVE_CEILING


def test_a_zero_limit_or_a_stray_key_is_not_a_body():
    with pytest.raises(ValidationError):
        PlanLimitsBody(**groups(tables={"csv_max_rows": 0, "csv_max_columns": 1}))
    with pytest.raises(ValidationError):
        PlanLimitsBody(**groups(images={**groups()["images"], "surprise": 1}))


def test_the_deployment_quota_is_the_plans_and_falls_back_to_the_yaml():
    assert asyncio.run(plan_limits.max_active_deployments(FakeConn(row=None), "t")) == settings.serving.max_active_deployments_per_tenant
    conn = FakeConn(rows={"FROM plan_limits": row(deployments={"max_active_deployments": 7})})
    assert asyncio.run(plan_limits.max_active_deployments(conn, "t")) == 7
    with pytest.raises(ValidationError):
        PlanLimitsBody(**groups(deployments={"max_active_deployments": 0}))
