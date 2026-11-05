"""What each plan tier may do, one `plan_limits` row per tier with a jsonb object per group of limits: how many imports and
training runs at once, how big a dataset can be and how big and how concurrent a /predict call. The YAML `dataset_limits` and `ray` sections are the fallback for a tier without a row and the ceiling
for concurrency; job timeout and Ray sizing are not per plan. Platform data, written only through /v1/admin/plan-limits.
"""
from datetime import datetime
from http import HTTPStatus

from pydantic import BaseModel, ConfigDict, Field

from app.core.enums import PlanTier
from app.core.problem import Problem
from app.core.settings import DatasetLimits, settings

LIMIT_ABOVE_CEILING = "limit_above_ceiling"


class _Group(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Concurrency(_Group):
    max_active_imports_per_tenant: int = Field(gt=0)
    max_concurrent_training_runs: int = Field(gt=0)


class Archives(_Group):
    upload_max_bytes: int = Field(gt=0)
    extracted_max_bytes: int = Field(gt=0)
    max_files: int = Field(gt=0)


class Images(_Group):
    image_max_dimension: int = Field(gt=0)
    image_max_bytes: int = Field(gt=0)


class Tables(_Group):
    csv_max_rows: int = Field(gt=0)
    csv_max_columns: int = Field(gt=0)


class HuggingFace(_Group):
    hf_max_repo_bytes: int = Field(gt=0)


class Predict(_Group):
    max_body_bytes: int = Field(gt=0)
    max_inflight_per_deployment: int = Field(gt=0)


class PlanLimitsBody(_Group):
    """What an admin writes for one tier; each field is a jsonb column of plan_limits, its keys named like DatasetLimits's."""

    concurrency: Concurrency
    archives: Archives
    images: Images
    tables: Tables
    huggingface: HuggingFace
    predict: Predict

    def dataset_overrides(self) -> dict[str, int]:
        """The values that replace DatasetLimits fields (training concurrency is not one of them)."""
        flat = {k: v for name in PlanLimitsBody.model_fields if name != "predict" for k, v in getattr(self, name).model_dump().items()}
        flat.pop("max_concurrent_training_runs")
        return flat


class PlanLimits(PlanLimitsBody):
    plan_tier: PlanTier
    updated_by: str
    updated_at: datetime


_COLUMNS = list(PlanLimitsBody.model_fields)
_SELECT = "SELECT * FROM plan_limits"
_FOR_TENANT = "SELECT p.* FROM plan_limits p JOIN tenants t ON t.plan_tier = p.plan_tier WHERE t.id = $1"
_UPSERT = (
    f"INSERT INTO plan_limits (plan_tier, {', '.join(_COLUMNS)}, updated_by) "
    f"VALUES ($1, {', '.join(f'${i + 2}' for i in range(len(_COLUMNS)))}, ${len(_COLUMNS) + 2}) "
    f"ON CONFLICT (plan_tier) DO UPDATE SET {', '.join(f'{c} = EXCLUDED.{c}' for c in _COLUMNS)}, "
    "updated_by = EXCLUDED.updated_by, updated_at = now() RETURNING *"
)


async def for_tenant(conn, tenant_id: str) -> PlanLimits | None:
    row = await conn.fetchrow(_FOR_TENANT, tenant_id)
    return None if row is None else PlanLimits.model_validate(dict(row))


async def dataset_limits(conn, tenant_id: str) -> DatasetLimits:
    """The YAML dataset limits with this tenant's plan values laid over them."""
    plan = await for_tenant(conn, tenant_id)
    return settings.dataset_limits if plan is None else settings.dataset_limits.model_copy(update=plan.dataset_overrides())


async def max_training_runs(conn, tenant_id: str) -> int:
    plan = await for_tenant(conn, tenant_id)
    return settings.ray.max_concurrent_training_runs if plan is None else plan.concurrency.max_concurrent_training_runs


async def predict_limits(conn, tenant_id: str) -> Predict:
    """The tenant's /predict limits; a tier without a row gets the serving section of the YAML."""
    plan = await for_tenant(conn, tenant_id)
    if plan is not None:
        return plan.predict
    return Predict(max_body_bytes=settings.serving.max_body_bytes, max_inflight_per_deployment=settings.serving.max_inflight_per_deployment)


async def list_all(conn) -> list[PlanLimits]:
    return [PlanLimits.model_validate(dict(r)) for r in await conn.fetch(f"{_SELECT} ORDER BY plan_tier")]


async def put(conn, tier: PlanTier, body: PlanLimitsBody, actor: str) -> PlanLimits:
    """Create or replace one tier's row. Concurrency may not exceed the YAML ceiling the queue horizon is computed from."""
    if body.concurrency.max_concurrent_training_runs > settings.ray.max_concurrent_training_runs:
        raise Problem(
            HTTPStatus.UNPROCESSABLE_ENTITY, LIMIT_ABOVE_CEILING,
            f"max_concurrent_training_runs is at most {settings.ray.max_concurrent_training_runs} (ray.max_concurrent_training_runs)")
    if body.predict.max_body_bytes > settings.serving.max_body_bytes:
        raise Problem(
            HTTPStatus.UNPROCESSABLE_ENTITY, LIMIT_ABOVE_CEILING,
            f"max_body_bytes is at most {settings.serving.max_body_bytes} (serving.max_body_bytes)")
    row = await conn.fetchrow(_UPSERT, tier, *(getattr(body, c).model_dump() for c in _COLUMNS), actor)
    return PlanLimits.model_validate(dict(row))
