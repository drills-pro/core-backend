"""Deployment rules and queries: deploy a model version, read, stop, start, delete, and the console's try-it log (answering is
predict.py and model_predict.py). Every status write is a move along an edge of DEPLOYMENTS (status.py) through job_lifecycle.advance."""
import secrets
import uuid
from http import HTTPStatus

import asyncpg

from app.core import audit, plan_limits
from app.core import task_types
from app.core.authz import AuthzContext, CallSource
from app.core.idempotency import insert_once, replay
from app.core.metrics import usage as usage_metrics
from app.core.problem import Problem, ProblemCode as KernelProblemCode, not_found
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
    MAIN_ROUTE_SUFFIX,
    PREDICTION_ID_PREFIX,
    NOT_DEPLOYABLE_DETAIL,
    ProblemCode,
    PLAN_TIER_RATE_LIMITS,
    PREDICT_KEY_BYTES,
    STOPPED_DETAIL,
    TOO_MANY_DEPLOYMENTS_DETAIL,
    PROMOTE_KEY_PREFIX,
    SETTING_DEPLOY_STRATEGY,
    VERSION_OR_RUN_DETAIL,
    DeployStrategy,
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
from app.modules.user.deployments.predict import hash_key
from app.modules.user.deployments.status import DEPLOYMENTS, LIVE_STATUSES, QUOTA_STATUSES, DeploymentStatus
from app.modules.user.projects import get_settings, get_task_type
from app.modules.user.tenants import get_plan_tier
from app.modules.user.training import get_run_model


def rate_limit_for(plan_tier: str | None) -> int:
    return PLAN_TIER_RATE_LIMITS.get(plan_tier, PLAN_TIER_RATE_LIMITS[PlanTier.FREE])


_LOCK_TENANT = "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))"
_ACTIVE_COUNT = "SELECT count(*) FROM deployments WHERE tenant_id = $1 AND id <> $2 AND status = ANY($3::text[])"
_BY_KEY = "SELECT * FROM deployments WHERE project_id = $1 AND idempotency_key = $2"
# A deployment serves one model version, which comes from one run (versions are 1:1 with runs today). Asked by the run (deprecated),
# it serves the newest version registered from it, NULL when none is.
_RUN_OF_VERSION = "SELECT training_run_id FROM model_versions WHERE id = $1 AND project_id = $2"
_VERSION_OF_RUN = "SELECT id FROM model_versions WHERE training_run_id = $1 AND project_id = $2 ORDER BY created_at DESC, id LIMIT 1"
_LIVE_OF_VERSION = "SELECT id, status FROM deployments WHERE model_version_id = $1 AND status = ANY($2::text[]) ORDER BY created_at DESC LIMIT 1"
_LIVE_STATUSES = [str(s) for s in LIVE_STATUSES]
_ACTIVE_OF_VERSION = "SELECT id FROM deployments WHERE model_version_id = $1 AND status = $2"
_INSERT = (
    "INSERT INTO deployments (id, tenant_id, project_id, training_run_id, ray_job_id, route_prefix, predict_key_hash, "
    "rate_limit_per_minute, serving, status, idempotency_key, risk_acknowledged, model_version_id) "
    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13) "
    "ON CONFLICT (project_id, idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING "
    "RETURNING id, status, route_prefix, rate_limit_per_minute, training_run_id, model_version_id"
)
_QUOTA_STATUSES = [str(s) for s in QUOTA_STATUSES]
_STATE_COLUMNS = "id, project_id, training_run_id, model_version_id, status, route_prefix, rate_limit_per_minute, created_at"


async def _check_quota(conn, tenant_id: str, deployment_id: uuid.UUID, *, lock: bool = False) -> None:
    """429 when the tenant's other active or deploying deployments already fill the plan's slots. `lock` serialises tenants'
    writes on an advisory lock first, so two concurrent creates cannot both take the last slot."""
    if lock:
        await conn.fetchval(_LOCK_TENANT, f"{DEPLOYMENT_LOCK_PREFIX}{tenant_id}")
    if await conn.fetchval(_ACTIVE_COUNT, tenant_id, deployment_id, _QUOTA_STATUSES) >= await plan_limits.max_active_deployments(conn, tenant_id):
        raise Problem(HTTPStatus.TOO_MANY_REQUESTS, ProblemCode.TOO_MANY_DEPLOYMENTS, TOO_MANY_DEPLOYMENTS_DETAIL)


def _replay(row, training_run_id: uuid.UUID) -> DeploymentCreated:
    """The deployment an earlier request with this key made. Only a hash of its predict key is stored, so it is not shown again."""
    same = replay(row, lambda r: r["training_run_id"] == training_run_id, ProblemCode.IDEMPOTENCY_MISMATCH)
    return DeploymentCreated.model_validate({**dict(same), "plan_tier": None})


async def create_deployment(
    ctx: AuthzContext, project_id: uuid.UUID, model_version_id: uuid.UUID | None, idempotency_key: str, acknowledge_risk: bool = False,
    training_run_id: uuid.UUID | None = None,
) -> DeploymentCreated:
    """Deploy a model version (`model_version_id`). `training_run_id` is the deprecated way to say the same, for one release: exactly one of the two."""
    if (model_version_id is None) == (training_run_id is None):
        raise Problem(HTTPStatus.UNPROCESSABLE_ENTITY, KernelProblemCode.VALIDATION_ERROR, VERSION_OR_RUN_DETAIL)
    async with TenantConn(ctx.tenant_id) as conn:
        if model_version_id is not None:
            training_run_id = await conn.fetchval(_RUN_OF_VERSION, model_version_id, project_id)
        else:
            model_version_id = await conn.fetchval(_VERSION_OF_RUN, training_run_id, project_id)
        if training_run_id is None:
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

        settings = await get_settings(conn, project_id)
        deploy_strategy = settings.get(SETTING_DEPLOY_STRATEGY, DeployStrategy.LATEST)

        # FR4/FR5 (06-project-settings-deployment.md): 'main' strategy has
        # at most one live deployment row per project — a re-deploy moves it
        # to the new run in place (same route, same predict key) instead of
        # minting a new one, so "promote this version to main" never breaks a
        # downstream integrator's URL. A deleted main row is never reused.
        existing_main = None
        if deploy_strategy == DeployStrategy.MAIN:
            existing_main = await conn.fetchrow(
                "SELECT id, status FROM deployments WHERE project_id = $1 AND route_prefix = $2 AND status <> $3",
                project_id,
                f"/predict/{project_id}/{MAIN_ROUTE_SUFFIX}",
                DeploymentStatus.DELETED,  # a deleted row has no key: the next deploy mints a new one
            )
            if existing_main and existing_main["status"] == DeploymentStatus.STOPPED:
                raise Problem(HTTPStatus.CONFLICT, ProblemCode.STOPPED, STOPPED_DETAIL)
        plan_tier = await get_plan_tier(conn, ctx.tenant_id)
        await _check_quota(conn, ctx.tenant_id, existing_main["id"] if existing_main else uuid.uuid4())  # cheap early answer; the one that counts is under the lock below

    # The predictor apps load the model on its first request: nothing to start, so the deployment is active at once.
    deployment_id = uuid.UUID(str(existing_main["id"])) if existing_main else uuid.uuid4()
    route_prefix = serve_client.route_prefix_for(str(deployment_id), str(project_id), deploy_strategy)
    return await _write_deployment(
        ctx, project_id, training_run_id, model_version_id, idempotency_key, deployment_id, existing_main, route_prefix, plan_tier, risky)


async def _write_deployment(
    ctx: AuthzContext, project_id: uuid.UUID, training_run_id: uuid.UUID, model_version_id: uuid.UUID | None, idempotency_key: str,
    deployment_id: uuid.UUID, existing_main, route_prefix: str, plan_tier, risk_acknowledged: bool = False,
) -> DeploymentCreated:
    """The write half of a deploy, under the tenant's deployments lock. One live deployment per version is the unique index's rule:
    a violation is 409. A `main` re-deploy moves the one row to the new version (and takes the new key)."""
    try:
        async with TenantConn(ctx.tenant_id) as conn:
            await _check_quota(conn, ctx.tenant_id, deployment_id, lock=True)
            if existing_main is not None:
                row = await job_lifecycle.advance(
                    conn, DEPLOYMENTS, dict(existing_main), DeploymentStatus.ACTIVE,
                    training_run_id=training_run_id, ray_job_id=None, serving=Serving.SHARED, idempotency_key=idempotency_key,
                    risk_acknowledged=risk_acknowledged,
                    model_version_id=model_version_id,
                )
                if row is None:  # moved since the read (stopped): a deploy never resurrects it
                    raise Problem(HTTPStatus.CONFLICT, ProblemCode.STOPPED, STOPPED_DETAIL)
                await audit.record_for(conn, ctx, project_id, AuditAction.REDEPLOY, EntityType.DEPLOYMENT, deployment_id)
                return DeploymentCreated.model_validate({**row, "plan_tier": plan_tier})

            # Shown once — this is the credential a customer's downstream
            # system uses to call /predict, separate from the tenant/JWT
            # scope used everywhere else. Only the hash is stored.
            predict_key = secrets.token_urlsafe(PREDICT_KEY_BYTES)
            row, created = await insert_once(
                conn, _INSERT,
                (deployment_id, ctx.tenant_id, project_id, training_run_id, None, route_prefix, hash_key(predict_key),
                 rate_limit_for(plan_tier), Serving.SHARED, DeploymentStatus.ACTIVE, idempotency_key, risk_acknowledged, model_version_id),
                _BY_KEY, (project_id, idempotency_key),
            )
            if not created:  # lost a race with the same key: that one is the deployment
                return _replay(row, training_run_id)
            await audit.record_for(conn, ctx, project_id, AuditAction.CREATE, EntityType.DEPLOYMENT, deployment_id)
            await job_lifecycle.emit(conn, DEPLOYMENTS, ctx.tenant_id, project_id, deployment_id, row["status"])
    except asyncpg.UniqueViolationError:  # only a unique index on the version or the run is left (deployments_one_active_per_version, deployments_one_live_per_run): the idempotency key conflict is DO NOTHING above
        raise Problem(HTTPStatus.CONFLICT, ProblemCode.DEPLOYMENT_EXISTS, DEPLOYMENT_EXISTS_DETAIL) from None
    usage_metrics.deployments_created.add(1)
    return DeploymentCreated.model_validate({**dict(row), "predict_key": predict_key, "plan_tier": plan_tier})


async def list_deployments(ctx: AuthzContext, project_id: uuid.UUID) -> list[DeploymentListItem]:
    async with TenantConn(ctx.tenant_id) as conn:
        rows = await conn.fetch(
            "SELECT id, status, route_prefix, created_at, model_version_id FROM deployments "
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
            "SELECT mv.model_id, mv.mlflow_version, r.report -> 'features' AS features, r.report -> 'categories' AS categories FROM deployments d "
            "JOIN model_versions mv ON mv.id = d.model_version_id JOIN training_runs r ON r.id = d.training_run_id "
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
        task_type=task, model_id=row["model_id"], model_revision=row["mlflow_version"],
        input=input_shape, output={"predictions": [item]},
        snippets=snippets.build(base_url, str(project_id), str(row["model_id"]), row["mlflow_version"], {"input": body_input}),
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


async def ensure_deployed(ctx: AuthzContext, project_id: uuid.UUID, version_id: uuid.UUID, acknowledge_risk: bool = False) -> uuid.UUID:
    """A deployment that serves `version_id`, active: the live one it has (a stopped one is started), or a new one under the project's
    deploy strategy. What a promotion deploys; raises the deploy's own Problem (quota, risk, no servable model) when it cannot."""
    async with TenantConn(ctx.tenant_id) as conn:
        live = await conn.fetchrow(_LIVE_OF_VERSION, version_id, _LIVE_STATUSES)
    if live is not None:
        if live["status"] == DeploymentStatus.STOPPED:
            await start_deployment(ctx, project_id, live["id"])
        return live["id"]
    created = await create_deployment(ctx, project_id, version_id, f"{PROMOTE_KEY_PREFIX}{version_id}:{uuid.uuid4().hex}", acknowledge_risk)
    return created.id


async def stop_version_deployments(ctx: AuthzContext, project_id: uuid.UUID, version_id: uuid.UUID) -> int:
    """Stop every active deployment of `version_id` (freeing its plan slot): what a promotion does to the version it replaced, so one
    version is live. Stopped rows are kept, so a rollback starts one again. Returns how many it stopped."""
    async with TenantConn(ctx.tenant_id) as conn:
        rows = await conn.fetch(_ACTIVE_OF_VERSION, version_id, DeploymentStatus.ACTIVE)
    for row in rows:
        await _move(ctx, project_id, row["id"], to=DeploymentStatus.STOPPED, action=AuditAction.STOP)
    return len(rows)


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
            await _check_quota(conn, ctx.tenant_id, deployment_id, lock=True)
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
