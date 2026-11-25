"""Deployment rules and queries: deploy a model version, read, stop, start, delete, and the console's try-it log (answering is
predict.py and model_predict.py). Every status write is a move along an edge of DEPLOYMENTS (status.py) through job_lifecycle.advance."""
import uuid
from http import HTTPStatus

import asyncpg

from app.core import audit, plan_limits
from app.core import task_types
from app.core.authz import AuthzContext, CallSource
from app.core.idempotency import Cap, check_cap, insert_capped, replay
from app.core.metrics import usage as usage_metrics
from app.core.problem import Problem, not_found
from app.core.tenant_conn import TenantConn
from app.integrations import job_lifecycle, serve_client
from app.modules.user.deployments import prediction_store as store, snippets
from app.modules.user.deployments.constants import (
    CATEGORY_PLACEHOLDER,
    CATEGORY_TYPE,
    FEATURE_TYPE,
    INPUT_FIELD_BY_ENCODER,
    NUMBER_PLACEHOLDER,
    OUTPUT_ITEM_BY_POSTPROCESS,
    TABULAR_ENCODER,
    SNIPPET_INPUT_PLACEHOLDER,
    DEPLOY_BELOW_FLOOR_DETAIL,
    DEPLOY_RISK_DETAIL,
    METRIC_FLOORS,
    DEPLOYMENT_EXISTS_DETAIL,
    DEPLOYMENT_LOCK_PREFIX,
    AuditAction,
    EntityType,
    PREDICTION_ID_PREFIX,
    NOT_DEPLOYABLE_DETAIL,
    ProblemCode,
    PLAN_TIER_RATE_LIMITS,
    TOO_MANY_DEPLOYMENTS_DETAIL,
    PlanTier,
    Serving,
)
from app.modules.user.deployments.schemas import (
    DeploymentSchema,
    DeploymentCreated,
    DeploymentDetail,
    DeploymentListItem,
    TestCall,
)
from app.modules.user.deployments.status import DEPLOYMENTS, QUOTA_STATUSES, DeploymentStatus
from app.modules.user.projects import get_task_type
from app.modules.user.tenants import get_plan_tier
from app.modules.user.training import get_run_model


def rate_limit_for(plan_tier: str | None) -> int:
    return PLAN_TIER_RATE_LIMITS.get(plan_tier, PLAN_TIER_RATE_LIMITS[PlanTier.FREE])


_ACTIVE_COUNT = "SELECT count(*) FROM deployments WHERE tenant_id = $1 AND id <> $2 AND status = ANY($3::text[])"
_CAP = Cap(DEPLOYMENT_LOCK_PREFIX, _ACTIVE_COUNT, plan_limits.max_active_deployments, ProblemCode.TOO_MANY_DEPLOYMENTS, TOO_MANY_DEPLOYMENTS_DETAIL)
_BY_KEY = "SELECT * FROM deployments WHERE project_id = $1 AND idempotency_key = $2"
# A deployment serves one model version, and a succeeded run has exactly one (training's settle registers it).
_VERSION_OF_RUN = "SELECT id FROM model_versions WHERE training_run_id = $1 AND project_id = $2 ORDER BY created_at DESC, id LIMIT 1"
_INSERT = (
    "INSERT INTO deployments (id, tenant_id, project_id, training_run_id, ray_job_id, route_prefix, "
    "rate_limit_per_minute, serving, status, idempotency_key, risk_acknowledged, model_version_id, main) "
    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13) "
    "ON CONFLICT (project_id, idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING "
    "RETURNING id, status, route_prefix, rate_limit_per_minute, training_run_id, model_version_id, main"
)
_QUOTA_STATUSES = [str(s) for s in QUOTA_STATUSES]
_STATE_COLUMNS = "id, project_id, training_run_id, model_version_id, status, route_prefix, rate_limit_per_minute, created_at, main"


def _replay(row, training_run_id: uuid.UUID) -> DeploymentCreated:
    """The deployment an earlier request with this key made. Only a hash of its predict key is stored, so it is not shown again."""
    same = replay(row, lambda r: r["training_run_id"] == training_run_id, ProblemCode.IDEMPOTENCY_MISMATCH)
    return DeploymentCreated.model_validate({**dict(same), "plan_tier": None})


async def create_deployment(
    ctx: AuthzContext, project_id: uuid.UUID, training_run_id: uuid.UUID, idempotency_key: str, acknowledge_risk: bool = False,
    set_main: bool = False,
) -> DeploymentCreated:
    """Deploy the model of a succeeded training run. `set_main` makes it the project's main deployment: the one a predict call that
    names no model is answered by (the previous main stops being it). A run with no test dataset is 409 deploy_risk_unacknowledged
    until acknowledge_risk=true."""
    async with TenantConn(ctx.tenant_id) as conn:
        model_version_id = await conn.fetchval(_VERSION_OF_RUN, training_run_id, project_id)
        if model_version_id is None:
            raise not_found()
        existing = await conn.fetchrow(_BY_KEY, project_id, idempotency_key)
        if existing is not None:
            return _replay(existing, training_run_id)
        run = await get_run_model(conn, project_id, training_run_id)
        if run is None:
            raise not_found()
        task = await get_task_type(conn, project_id)
        info = None if task is None else await task_types.get(conn, task)
        if info is None or not info.enabled or not info.servable or not run.servable_by(info.postprocess):
            raise Problem(HTTPStatus.CONFLICT, ProblemCode.NOT_DEPLOYABLE, NOT_DEPLOYABLE_DETAIL)
        risk = None if run.has_test_split else DEPLOY_RISK_DETAIL
        metric, floor = METRIC_FLOORS.get(task, (None, 0.0))
        score = (run.metrics or {}).get(metric)
        if risk is None and isinstance(score, int | float) and score < floor:
            risk = DEPLOY_BELOW_FLOOR_DETAIL.format(metric=metric, value=score, floor=floor)
        risky = risk is not None
        if risky and not acknowledge_risk:  # a warning with an acknowledgement, not a block (planning/06 Quality gates)
            raise Problem(HTTPStatus.CONFLICT, ProblemCode.DEPLOY_RISK, risk)
        plan_tier = await get_plan_tier(conn, ctx.tenant_id)
        await check_cap(conn, _CAP, ctx.tenant_id, uuid.uuid4(), _QUOTA_STATUSES)  # cheap early answer; the one that counts is under the lock below

    deployment_id = uuid.uuid4()
    # The predictor apps load the model on its first request: nothing to start, so the deployment is active at once.
    return await _write_deployment(
        ctx, project_id, training_run_id, model_version_id, idempotency_key, deployment_id, plan_tier, risky, set_main)


async def _write_deployment(
    ctx: AuthzContext, project_id: uuid.UUID, training_run_id: uuid.UUID, model_version_id: uuid.UUID, idempotency_key: str,
    deployment_id: uuid.UUID, plan_tier, risk_acknowledged: bool, set_main: bool,
) -> DeploymentCreated:
    """The write half of a deploy, under the tenant's deployments lock. One live deployment per version is the unique index's rule:
    a violation is 409. Setting main first clears the project's previous main, so the one-main index holds."""
    try:
        async with TenantConn(ctx.tenant_id) as conn:
            if set_main:
                await conn.execute("UPDATE deployments SET main = false WHERE project_id = $1 AND main", project_id)
            row, created = await insert_capped(
                conn, _CAP, ctx.tenant_id, (deployment_id, _QUOTA_STATUSES), _INSERT,
                (deployment_id, ctx.tenant_id, project_id, training_run_id, None, serve_client.route_prefix_for(str(deployment_id)),
                 rate_limit_for(plan_tier), Serving.SHARED, DeploymentStatus.ACTIVE, idempotency_key, risk_acknowledged,
                 model_version_id, set_main),
                _BY_KEY, (project_id, idempotency_key),
            )
            if not created:  # lost a race with the same key: that one is the deployment
                return _replay(row, training_run_id)
            await audit.record_for(conn, ctx, project_id, AuditAction.CREATE, EntityType.DEPLOYMENT, deployment_id)
            await job_lifecycle.emit(conn, DEPLOYMENTS, ctx.tenant_id, project_id, deployment_id, row["status"])
    except asyncpg.UniqueViolationError:  # a unique index on the version, the run or the main slot is left: the idempotency key conflict is DO NOTHING above
        raise Problem(HTTPStatus.CONFLICT, ProblemCode.DEPLOYMENT_EXISTS, DEPLOYMENT_EXISTS_DETAIL) from None
    usage_metrics.deployments_created.add(1)
    return DeploymentCreated.model_validate({**dict(row), "plan_tier": plan_tier})


async def set_main(ctx: AuthzContext, project_id: uuid.UUID, deployment_id: uuid.UUID) -> DeploymentDetail:
    """Make a live deployment the project's main one: the previous main is cleared first (two statements, for the one-main index)."""
    async with TenantConn(ctx.tenant_id) as conn:
        row = await conn.fetchrow(
            f"SELECT {_STATE_COLUMNS} FROM deployments WHERE id = $1 AND project_id = $2 AND status <> $3 FOR UPDATE",  # noqa: S608
            deployment_id, project_id, DeploymentStatus.DELETED,
        )
        if row is None:
            raise not_found()
        await conn.execute("UPDATE deployments SET main = false WHERE project_id = $1 AND main AND id <> $2", project_id, deployment_id)
        await conn.execute("UPDATE deployments SET main = true WHERE id = $1", deployment_id)
        await audit.record_for(conn, ctx, project_id, AuditAction.SET_MAIN, EntityType.DEPLOYMENT, deployment_id)
        row = await conn.fetchrow(f"SELECT {_STATE_COLUMNS} FROM deployments WHERE id = $1", deployment_id)  # noqa: S608
    return DeploymentDetail.model_validate(dict(row))


async def list_deployments(ctx: AuthzContext, project_id: uuid.UUID) -> list[DeploymentListItem]:
    async with TenantConn(ctx.tenant_id) as conn:
        rows = await conn.fetch(
            "SELECT id, status, route_prefix, created_at, model_version_id, main FROM deployments "
            "WHERE project_id = $1 AND status <> $2 ORDER BY created_at DESC",
            project_id,
            DeploymentStatus.DELETED,
        )
    return [DeploymentListItem.model_validate(dict(r)) for r in rows]


async def get_deployment(ctx: AuthzContext, project_id: uuid.UUID, deployment_id: uuid.UUID) -> DeploymentDetail:
    async with TenantConn(ctx.tenant_id) as conn:
        # Explicit columns: never predict_key_hash / ray_job_id. Read-only.
        row = await conn.fetchrow(
            f"SELECT {_STATE_COLUMNS} FROM deployments WHERE id = $1 AND project_id = $2 AND status <> $3",  # noqa: S608
            deployment_id,
            project_id,
            DeploymentStatus.DELETED,
        )
    if row is None:
        raise not_found()
    return DeploymentDetail.model_validate(dict(row))


async def get_schema(ctx: AuthzContext, project_id: uuid.UUID, deployment_id: uuid.UUID, base_url: str) -> DeploymentSchema:
    """The input and output shape for POST /v1/predict on this deployment, from its task type's encoder and postprocess, and call snippets."""
    async with TenantConn(ctx.tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT r.report -> 'features' AS features, r.report -> 'categories' AS categories FROM deployments d "
            "JOIN training_runs r ON r.id = d.training_run_id "
            "WHERE d.id = $1 AND d.project_id = $2 AND d.status <> $3",
            deployment_id, project_id, DeploymentStatus.DELETED,
        )
        task = await get_task_type(conn, project_id)
        info = None if task is None else await task_types.get(conn, task)
    if row is None or info is None:
        raise not_found()
    if info.encoder == TABULAR_ENCODER:  # one value per feature column, in the model's own order
        columns, categories = row["features"] or [], row["categories"] or {}  # a run from before categories has none
        input_shape = {name: CATEGORY_TYPE if name in categories else FEATURE_TYPE for name in columns}
        body_input = {name: CATEGORY_PLACEHOLDER if name in categories else NUMBER_PLACEHOLDER for name in columns}
    else:
        field = INPUT_FIELD_BY_ENCODER.get(info.encoder)
        input_shape = {field: "string"} if field else {}
        body_input = {field: SNIPPET_INPUT_PLACEHOLDER} if field else {}
    item = OUTPUT_ITEM_BY_POSTPROCESS.get(info.postprocess, {})
    return DeploymentSchema(
        task_type=task, deployment_id=deployment_id, input=input_shape, output={"predictions": [item]},
        snippets=snippets.build(base_url, str(project_id), str(deployment_id), {"input": body_input}),
    )


_CONSOLE_CALLS = "SELECT * FROM predictions WHERE deployment_id = $1 AND source = $2 ORDER BY created_at DESC LIMIT $3"


async def list_test_calls(ctx: AuthzContext, project_id: uuid.UUID, deployment_id: uuid.UUID, limit: int) -> list[TestCall]:
    """The try-it log: the deployment's predictions made from the console (source=console), newest first. The console calls POST /v1/predict
    like any client, so these are real stored predictions, metered like any call."""
    async with TenantConn(ctx.tenant_id) as conn:
        owned = await conn.fetchval(
            "SELECT 1 FROM deployments WHERE id = $1 AND project_id = $2 AND status <> $3",
            deployment_id, project_id, DeploymentStatus.DELETED,
        )
        if not owned:
            raise not_found()
        rows = await conn.fetch(_CONSOLE_CALLS, deployment_id, CallSource.CONSOLE, limit)
    return [
        TestCall(
            id=f"{PREDICTION_ID_PREFIX}{r['id']}", status=r["status"], request=None if r["purged_at"] else {"input": await store.payload(r, "input")},
            response=None if r["purged_at"] else await store.payload(r, "output"), error=r["error"] or None, latency_ms=r["latency_ms"], created_at=r["created_at"])
        for r in rows
    ]


# ---- lifecycle: stop, start, delete ----



async def _move(
    ctx: AuthzContext, project_id: uuid.UUID, deployment_id: uuid.UUID, *, to: DeploymentStatus, action: AuditAction,
    only_from: tuple[DeploymentStatus, ...] | None = None,
) -> tuple[dict, bool]:
    """Move the deployment to `to` along an edge of DEPLOYMENTS (narrowed to `only_from` when given), audited in the same
    transaction. Returns (row, moved): a row already at `to` comes back with moved False (a repeat is not an error); no edge
    from its status is 409, a missing or deleted row 404. The row is locked from the read to the move. Deleting clears the
    predict key, so it can never match again."""
    async with TenantConn(ctx.tenant_id) as conn:
        if to in QUOTA_STATUSES:  # starting takes a slot back: stopped freed it
            await check_cap(conn, _CAP, ctx.tenant_id, deployment_id, _QUOTA_STATUSES, lock=True)
        row = await conn.fetchrow(
            f"SELECT {_STATE_COLUMNS} FROM deployments WHERE id = $1 AND project_id = $2 AND status <> $3 FOR UPDATE",  # noqa: S608
            deployment_id, project_id, DeploymentStatus.DELETED,
        )
        if row is None:
            raise not_found()
        if row["status"] == to:
            return dict(row), False
        cols = {"predict_key_hash": None} if to == DeploymentStatus.DELETED else {}
        moved = None if only_from and row["status"] not in only_from else await job_lifecycle.advance(conn, DEPLOYMENTS, dict(row), to, **cols)
        if moved is None:
            raise Problem(HTTPStatus.CONFLICT, ProblemCode.INVALID_TRANSITION, f"a {row['status']} deployment cannot {action}")
        await audit.record_for(conn, ctx, project_id, action, EntityType.DEPLOYMENT, deployment_id)
    return {k: moved[k] for k in row.keys()}, True


async def stop_deployment(ctx: AuthzContext, project_id: uuid.UUID, deployment_id: uuid.UUID) -> DeploymentDetail:
    """active -> stopped. Predict and try-it answer 409 until it starts again; the predict key is kept."""
    row, _ = await _move(ctx, project_id, deployment_id, to=DeploymentStatus.STOPPED, action=AuditAction.STOP)
    return DeploymentDetail.model_validate(row)


async def delete_deployment(ctx: AuthzContext, project_id: uuid.UUID, deployment_id: uuid.UUID) -> None:
    """Soft delete: the row stays for lineage, its predict key can never match again. Nothing runs of its own to stop."""
    await _move(ctx, project_id, deployment_id, to=DeploymentStatus.DELETED, action=AuditAction.DELETE)


async def start_deployment(ctx: AuthzContext, project_id: uuid.UUID, deployment_id: uuid.UUID) -> DeploymentDetail:
    """stopped -> active, served on the shared predictor apps: same row, same key. Only a stopped one starts (a failed leftover
    legacy row is re-deployed instead)."""
    row, _ = await _move(
        ctx, project_id, deployment_id, to=DeploymentStatus.ACTIVE, action=AuditAction.START, only_from=(DeploymentStatus.STOPPED,))
    return DeploymentDetail.model_validate(row)
