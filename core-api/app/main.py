"""Composition root.

The only place that knows the full list of modules. It wires each module's
router into one FastAPI app and starts the background pollers; it contains
no domain logic of its own — everything below /v1 lives in app/modules/*.

Layering, enforced by tests/test_architecture.py:

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
from fastapi.staticfiles import StaticFiles
from prometheus_client import make_asgi_app

from app import background
from app.core import tracing
from app.modules.admin import tenants as admin_tenants
from app.modules.user import (
    auth,
    datasets,
    deployments,
    health,
    labeling,
    model_registry,
    preprocessing,
    projects,
    public,
    teams,
    training,
    usage,
    users,
)

# Registration order is significant: FastAPI matches routes in the order
# they are added, so the more specific prefixes stay ahead of the ones that
# could shadow them.
ROUTERS = (
    admin_tenants.router,
    # After the listing, never before it: the detail router owns
    # /v1/admin/tenants/{tenant_id}, which would otherwise swallow the
    # listing's own /v1/admin/tenants/summary.
    admin_tenants.detail_router,
    projects.router,
    datasets.router,
    deployments.router,
    auth.router,
    labeling.router,
    model_registry.router,
    projects.members_router,
    preprocessing.router,
    public.router,
    teams.router,
    training.router,
    users.router,
    usage.router,
    health.router,
)


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    tasks = background.start()
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
    for router in ROUTERS:
        app.include_router(router)
    app.mount("/ui", StaticFiles(directory="ui", html=True), name="ui")
    app.mount("/metrics", make_asgi_app())
    return app


app = create_app()
