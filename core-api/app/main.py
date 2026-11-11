"""Composition root.

The only place that knows the full list of modules. It wires each module's
router into one FastAPI app and starts the background pollers; it contains
no domain logic of its own — everything below /v1 lives in app/modules/*.

Layering, enforced by tests/architecture/test_architecture.py:

    modules  ->  integrations  ->  core
       |                            ^
       +----------------------------+

core/         shared kernel: config, db pool, tenancy, auth, audit, telemetry
integrations/ clients for the systems outside this process (Ray, LakeFS, ...)
modules/      bounded contexts, grouped by audience (user/, admin/). One package
              per context, talking to the outside world only through its own
              router and to each other only through the package's public
              API (its __init__).
"""
import contextlib

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import background
from app.core import auth_log, problem, tracing
from app.core.settings import settings
from app.modules.admin import activity as admin_activity
from app.modules.admin import job_packages as admin_job_packages
from app.modules.admin import models as admin_models
from app.modules.admin import presets as admin_presets
from app.modules.admin import plan_limits as admin_plan_limits
from app.modules.admin import task_types as admin_task_types
from app.modules.admin import tenants as admin_tenants
from app.modules.user import (
    api_keys,
    auth,
    datasets,
    deployments,
    health,
    model_registry,
    projects,
    public,
    teams,
    tenant_members,
    tenants,
    training,
    usage,
)
from app.modules.user.datasets import pipeline_log

# Registration order is significant: FastAPI matches routes in the order
# they are added, so the more specific prefixes stay ahead of the ones that
# could shadow them.
ROUTERS = (
    admin_activity.router,
    admin_job_packages.router,
    admin_models.router,
    admin_presets.router,
    admin_task_types.router,
    admin_plan_limits.router,
    admin_tenants.router,
    # After the listing, never before it: the detail router owns
    # /v1/admin/tenants/{tenant_id}, which would otherwise swallow the
    # listing's own /v1/admin/tenants/summary.
    admin_tenants.detail_router,
    projects.router,
    datasets.router,
    deployments.router,
    auth.router,
    training.models_router,
    model_registry.router,
    projects.members_router,
    api_keys.router,
    public.router,
    teams.router,
    tenant_members.router,
    tenants.router,
    training.events_router,
    training.presets_router,
    training.router,
    usage.router,
    health.router,
)


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    tasks = background.start() if settings.background.pollers_in_api else []
    try:
        yield
    finally:
        await background.stop(tasks)


def create_app() -> FastAPI:
    # Telemetry is configured manually below (app/core/tracing.py, app/core/metrics.py) —
    # FastAPI's own auto-detection sees the OpenTelemetry packages installed for that and
    # tries to self-configure too, which errors without the fastapi[opentelemetry] extra.
    app = FastAPI(title="core-api", version="0.1.0", lifespan=lifespan, telemetry={"auto_configure": False})
    app.add_middleware(
        CORSMiddleware,
        # Wildcard is safe here: the frontend authenticates with a Bearer
        # header, not cookies, so allow_credentials stays off (browsers
        # reject "*" + credentials combos anyway).
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    tracing.setup(app)
    auth_log.setup()
    pipeline_log.setup()
    problem.install(app)
    for router in ROUTERS:
        app.include_router(router)
    return app


app = create_app()
