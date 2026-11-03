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

SQL = next((Path(__file__).parents[2] / "migrations").glob("*_plan_limits.sql")).read_text()


def groups(**over) -> dict:
    """A plan's limits as the YAML has them today, grouped as the table groups them; `over` replaces whole groups."""
    values = {**settings.dataset_limits.model_dump(), "max_concurrent_training_runs": settings.ray.max_concurrent_training_runs}
    return {name: {k: values[k] for k in model.model_fields}
            for name, model in ((n, f.annotation) for n, f in PlanLimitsBody.model_fields.items())} | over


def row(**over) -> dict:
    return {"plan_tier": PlanTier.PRO, "updated_by": "ops", "updated_at": datetime.now(UTC), **groups(), **over}


def test_the_columns_and_the_tier_check_mirror_the_models_and_the_enum():
    columns = re.findall(r"^  (\w+) jsonb NOT NULL CHECK \(jsonb_typeof\(\1\) = 'object'\)", SQL, re.M)
    assert columns == list(PlanLimitsBody.model_fields)
    tiers = re.search(r"plan_tier text PRIMARY KEY CHECK \(plan_tier IN \(([^)]*)\)", SQL).group(1)
    assert set(re.findall(r"'([^']+)'", tiers)) == {t.value for t in PlanTier}


def test_every_tier_is_seeded_with_the_yaml_values():
    seeded = {name: json.loads(text) for name, text in re.findall(r"^\s+'(\{.*\})'::jsonb,?$", SQL, re.M) and zip(
        PlanLimitsBody.model_fields, re.findall(r"^\s+'(\{.*\})'::jsonb,?$", SQL, re.M))}
    assert seeded == groups()
    assert set(re.findall(r"ARRAY\[([^\]]*)\]", SQL)[0].replace("'", "").split(", ")) == {t.value for t in PlanTier}


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


def test_a_zero_limit_or_a_stray_key_is_not_a_body():
    with pytest.raises(ValidationError):
        PlanLimitsBody(**groups(tables={"csv_max_rows": 0, "csv_max_columns": 1}))
    with pytest.raises(ValidationError):
        PlanLimitsBody(**groups(images={**groups()["images"], "surprise": 1}))
