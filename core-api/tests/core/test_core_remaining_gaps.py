"""Core modules' remaining branches: auth configuration and the userinfo bound, bearer tenancy, db pool setup and
rotation, job_package rows, the metric name rule and the exporters wired outside tests."""
import asyncio
import datetime
import json
import uuid
from types import SimpleNamespace

import httpx
import pytest

from app.core import auth, db, job_packages, tenancy, tracing
from app.core.authz import deps
from app.core.enums import JobPackageName, PackageStatus, SmokeStatus
from app.core.metrics import base
from app.core.problem import Problem
from app.core.settings import settings
from tests.support import FakeConn, TenantCtx

SUB = "user-1"


# --- auth ---------------------------------------------------------------------------------------------------------

def test_an_unset_project_id_fails_closed_with_a_500(monkeypatch):
    monkeypatch.setattr(settings.zitadel, "project_id", "")
    with pytest.raises(Problem) as err:
        auth.project_id()
    assert (err.value.status_code, err.value.code) == (500, "auth_not_configured")


def test_the_userinfo_cache_is_emptied_before_it_outgrows_its_bound(monkeypatch):
    stale = {f"tok-{i}": (float("inf"), {"email": "old"}) for i in range(1001)}
    monkeypatch.setattr(auth, "_userinfo_cache", stale)
    real = httpx.AsyncClient
    monkeypatch.setattr(auth.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"email": "a@b.co"})), **kw))
    claims = asyncio.run(auth.get_profile_claims(auth.Claims({"sub": SUB}, "fresh")))
    assert claims.email == "a@b.co"
    assert list(auth._userinfo_cache) == ["fresh"]


# --- tenancy ------------------------------------------------------------------------------------------------------

@pytest.fixture
def bearer(monkeypatch):
    """A verified token for SUB; Postgres answers member_role from `roles` ({tenant_id: role})."""
    roles = {}

    class Conn(FakeConn):
        async def fetchval(self, sql, sub, tenant_id):
            return roles.get(tenant_id)

    monkeypatch.setattr(tenancy, "verify_token", lambda token: auth.Claims({"sub": SUB}, token))

    async def get_pool():
        return SimpleNamespace(acquire=lambda: TenantCtx(Conn()))

    monkeypatch.setattr(tenancy, "get_pool", get_pool)
    return roles


def test_a_verified_member_acts_in_the_tenant_they_name(bearer):
    tenant = uuid.uuid4()
    bearer[tenant] = "owner"
    assert asyncio.run(tenancy.get_tenant_id(authorization="Bearer t", x_tenant_id=str(tenant))) == str(tenant)


def test_a_verified_non_member_gets_a_404_not_a_hint_that_the_tenant_exists(bearer):
    with pytest.raises(Problem) as err:
        asyncio.run(tenancy.get_tenant_id(authorization="Bearer t", x_tenant_id=str(uuid.uuid4())))
    assert err.value.status_code == 404


def test_the_actor_of_a_bearer_request_is_the_token_subject_not_a_header(bearer):
    assert asyncio.run(tenancy.get_actor(authorization="Bearer t", x_user_id="someone-else")) == SUB


def test_a_non_bearer_authorization_falls_through_to_the_dev_headers(monkeypatch):
    monkeypatch.setattr(settings.auth, "dev_headers", True)
    assert asyncio.run(tenancy.get_tenant_id(authorization="Basic abc", x_tenant_id="dev-tenant")) == "dev-tenant"


# --- authz deps ---------------------------------------------------------------------------------------------------

def test_a_project_id_that_is_not_a_uuid_is_a_404():
    with pytest.raises(Problem) as err:
        deps._parse_uuid("not-a-uuid")
    assert err.value.status_code == 404
    good = uuid.uuid4()
    assert deps._parse_uuid(str(good)) == good


# --- db -----------------------------------------------------------------------------------------------------------

def test_every_connection_decodes_jsonb_into_python_objects(monkeypatch):
    opened = {}

    async def create_pool(dsn, init, **kw):
        opened.update(dsn=dsn, init=init, **kw)
        return SimpleNamespace(close=lambda: None)

    monkeypatch.setattr(db.asyncpg, "create_pool", create_pool)
    monkeypatch.setattr(db, "_pool", None)
    monkeypatch.setattr(db, "_pool_creds", None)

    asyncio.run(db._rotate({"username": "u", "password": "p"}))
    assert opened["dsn"].startswith("postgresql://u:p@") and opened["min_size"] == db.PG.pool_min

    seen = {}

    class Conn:
        async def set_type_codec(self, name, **kw):
            seen.update(name=name, **kw)

    asyncio.run(opened["init"](Conn()))
    assert seen["name"] == "jsonb" and seen["schema"] == "pg_catalog"
    assert seen["decoder"]('{"a": 1}') == {"a": 1} and json.loads(seen["encoder"]({"a": 1})) == {"a": 1}


def test_a_failed_credential_rotation_keeps_the_watcher_running(monkeypatch, caplog):
    ticks = []

    async def sleep(seconds):
        ticks.append(seconds)
        if len(ticks) == 3:
            raise asyncio.CancelledError

    def get(name):
        if len(ticks) == 1:
            raise OSError("secret file unreadable")
        return {"username": "same"}

    monkeypatch.setattr(db.asyncio, "sleep", sleep)
    monkeypatch.setattr(db.secret_files, "get", get)
    monkeypatch.setattr(db, "_pool_creds", {"username": "same"})
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(db._watch())
    assert ticks == [db.WATCH_SECONDS] * 3  # the error on tick 1 did not end the loop
    assert "rotation failed" in caplog.text


# --- core.job_packages --------------------------------------------------------------------------------------------

def row(**kw):
    return dict(name=JobPackageName.RAY_JOBS, sha256="a" * 64, s3_key="jobs/x.zip", status=PackageStatus.CANDIDATE, published_by="ci",
                updated_by="ci", updated_at=datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc), smoke_status=SmokeStatus.PENDING,
                smoke_job_id=None, **kw)


def test_a_names_packages_are_listed_newest_first():
    conn = FakeConn(rowset=[row(), row()])
    packages = asyncio.run(job_packages.list_for(conn, JobPackageName.RAY_JOBS))
    sql, args = conn.calls[0]
    assert len(packages) == 2 and "ORDER BY updated_at DESC" in sql and args == (JobPackageName.RAY_JOBS,)


def test_a_smoke_job_id_is_only_set_once():
    conn = FakeConn()
    asyncio.run(job_packages.start_smoke(conn, JobPackageName.RAY_JOBS, "a" * 64, "smoke-1"))
    sql, args = conn.calls[0]
    assert "smoke_job_id IS NULL" in sql and args == (JobPackageName.RAY_JOBS, "a" * 64, "smoke-1")


def test_settling_a_smoke_returns_the_updated_package():
    conn = FakeConn(row=row() | {"smoke_status": SmokeStatus.PASSED})
    package = asyncio.run(job_packages.set_smoke(conn, JobPackageName.RAY_JOBS, "a" * 64, SmokeStatus.PASSED))
    assert package.smoke_status == SmokeStatus.PASSED and conn.calls[0][1][2] == SmokeStatus.PASSED


def test_a_failed_smoke_is_reset_by_a_compare_and_set_on_candidate_and_failed():
    conn = FakeConn(row=row())
    package = asyncio.run(job_packages.reset_failed_smoke(conn, JobPackageName.RAY_JOBS, "a" * 64))
    sql, args = conn.calls[0]
    assert package.smoke_job_id is None and "smoke_job_id = NULL" in sql and "status = $3 AND smoke_status = $4" in sql
    assert args == (JobPackageName.RAY_JOBS, "a" * 64, PackageStatus.CANDIDATE, SmokeStatus.FAILED, SmokeStatus.PENDING)


def test_a_smoke_that_is_not_failed_is_not_reset():
    assert asyncio.run(job_packages.reset_failed_smoke(FakeConn(row=None), JobPackageName.RAY_JOBS, "a" * 64)) is None


# --- metrics and tracing outside the test environment ---------------------------------------------------------------

@pytest.mark.parametrize("name", ["Bad.Name.x", "only_one_segment", "has-dash.ns.name", "9starts.ns.name", "trailing."])
def test_a_metric_name_outside_the_convention_is_refused_at_definition(name):
    with pytest.raises(ValueError, match="must be"):
        base._full_name(name)


def test_a_conforming_metric_name_gets_the_service_prefix():
    assert base._full_name("auth.sign_in.count") == f"{base.PREFIX}.auth.sign_in.count"


def test_outside_tests_metrics_are_exported_over_otlp_on_an_interval(monkeypatch):
    made = {}

    class Exporter:
        def __init__(self, endpoint, insecure):
            made["exporter"] = (endpoint, insecure)

    def reader(exporter, export_interval_millis):
        made["reader"] = (exporter, export_interval_millis)
        made["reader_obj"] = SimpleNamespace()  # never started: no thread, no network
        return made["reader_obj"]

    monkeypatch.setattr(settings, "env", "prod")
    monkeypatch.setattr(base, "OTLPMetricExporter", Exporter)
    monkeypatch.setattr(base, "PeriodicExportingMetricReader", reader)
    monkeypatch.setattr(base, "MeterProvider", lambda resource, metric_readers: SimpleNamespace(resource=resource, readers=metric_readers))
    provider = base._build_provider()
    assert made["exporter"] == (settings.observability.otlp_endpoint, True)
    assert made["reader"][1] == base.EXPORT_INTERVAL_MILLIS and provider.readers == [made["reader_obj"]]
    assert provider.resource.attributes["service.name"] == base.SERVICE_NAME


def test_outside_tests_spans_are_batched_to_the_collector(monkeypatch):
    added = []
    monkeypatch.setattr(settings, "env", "prod")
    monkeypatch.setattr(tracing, "OTLPSpanExporter", lambda endpoint, insecure: ("exporter", endpoint, insecure))
    monkeypatch.setattr(tracing, "BatchSpanProcessor", lambda exporter: ("processor", exporter))
    monkeypatch.setattr(tracing.TracerProvider, "add_span_processor", lambda self, processor: added.append(processor))
    monkeypatch.setattr(tracing.trace, "set_tracer_provider", lambda provider: None)
    noop = SimpleNamespace(instrument_app=lambda app: None, instrument=lambda: None)
    monkeypatch.setattr(tracing, "FastAPIInstrumentor", noop)
    monkeypatch.setattr(tracing, "HTTPXClientInstrumentor", lambda: noop)
    monkeypatch.setattr(tracing, "AsyncPGInstrumentor", lambda: noop)
    tracing.setup(object())
    assert added == [("processor", ("exporter", tracing.OTLP_ENDPOINT, True))]
