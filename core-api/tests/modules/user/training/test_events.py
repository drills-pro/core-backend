"""Progress events (audit C-07): the trainer's token, the internal route, the stored points, and GET's `progress`."""
import asyncio
import json
import re
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.core.settings import settings
from app.integrations.job_spec import MAX_EPOCHS
from app.modules.user.training import events_service, run_service
from app.modules.user.training.constants import (
    EVENTS_TOKEN_ENV,
    MAX_EVENT_BODY_BYTES,
    MAX_EVENT_METRICS,
    EventType,
    TrainingStatus,
)
from app.modules.user.training.routes import events_router
from app.modules.user.training.schemas import TrainingRun
from tests.migrations.test_tenant_rls import MIGRATIONS, schema_after
from tests.modules.user.training.test_training import PROJECT, Gateway, create, make_conn, run_row
from tests.support import FakeConn, TenantCtx, make_client

RUN, TENANT = uuid.uuid4(), uuid.uuid4()
TOKEN = "t0ken-secret"
URL = f"/internal/v1/training-runs/{RUN}/events"
EVENT = {"type": "progress", "seq": 0, "epoch": 1, "epochs": 3, "metrics": {"train_loss": 0.5, "accuracy": 0.9}}
AUTH = {"Authorization": f"Bearer {TOKEN}"}
BASE_URL = "http://core-api.test:8090"


class Pool:
    def __init__(self, row):
        self.row = row

    def acquire(self):
        return TenantCtx(SimpleNamespace(fetchrow=self._fetchrow))

    async def _fetchrow(self, sql, run_id):
        assert "training_run_events_auth" in sql
        return self.row


@pytest.fixture
def channel(monkeypatch):
    """A run with an events channel: the auth lookup answers its tenant and the hash of TOKEN; the tenant connection is
    `conn`, whose status and insert answers each test sets."""
    row = {"tenant_id": TENANT, "token_hash": events_service.hash_token(TOKEN)}

    async def pool():
        return Pool(row)

    monkeypatch.setattr(events_service, "get_pool", pool)
    conn = FakeConn(vals={"SELECT status": TrainingStatus.RUNNING, "INSERT INTO training_run_events": 0})
    monkeypatch.setattr(events_service, "TenantConn", lambda tenant_id: TenantCtx(conn))
    conn.auth_row = row
    return conn


@pytest.fixture
def client():
    return make_client(events_router, headers={})


def post(client, body=EVENT, headers=AUTH):
    return client.post(URL, content=json.dumps(body) if not isinstance(body, bytes) else body, headers={"Content-Type": "application/json", **headers})


# ---- authentication ----

@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN + "x"}, {"Authorization": "Bearer "}])
def test_a_missing_or_wrong_token_is_401(channel, client, headers):
    assert post(client, headers=headers).status_code == 401
    assert not [c for c in channel.calls if "training_run_events" in c[0]]  # nothing was read or written


def test_a_run_without_a_channel_or_that_does_not_exist_is_the_same_401(channel, client, monkeypatch):
    async def none():
        return Pool(None)  # the lookup finds no row: unknown run, or a run whose events_token_hash is null

    monkeypatch.setattr(events_service, "get_pool", none)
    assert post(client).status_code == 401


def test_the_token_of_another_run_is_refused(channel, client):
    channel.auth_row["token_hash"] = events_service.hash_token("another-runs-token")
    assert post(client).status_code == 401


def test_the_route_is_public_and_needs_no_user_session(channel, client):
    assert post(client).status_code == 201  # no X-Tenant-Id, no user: the token is the credential


# ---- storing ----

def test_a_progress_event_is_stored_for_the_runs_tenant(channel, client):
    r = post(client)
    assert (r.status_code, r.json()) == (201, {"seq": 0, "duplicate": False})
    (args,) = [a for sql, a in channel.calls if sql.startswith("INSERT INTO training_run_events")]
    assert args == (RUN, 0, str(TENANT), EventType.PROGRESS, 1, 3, EVENT["metrics"])


def test_a_repeated_seq_is_a_200_no_op(channel, client):
    channel.vals["INSERT INTO training_run_events"] = None  # ON CONFLICT DO NOTHING returned no row
    r = post(client)
    assert (r.status_code, r.json()) == (200, {"seq": 0, "duplicate": True})


@pytest.mark.parametrize("status", [s for s in TrainingStatus if s is not TrainingStatus.RUNNING])
def test_only_a_running_run_takes_events(channel, client, status):
    channel.vals["SELECT status"] = status
    assert post(client).status_code == 409
    assert not [c for c in channel.calls if c[0].startswith("INSERT")]


def test_the_status_read_locks_the_row_so_a_settle_cannot_overtake_an_event(channel, client):
    post(client)
    assert next(sql for sql, _ in channel.calls if sql.startswith("SELECT status")).endswith("FOR SHARE")


# ---- untrusted input ----

def metrics(n):
    return {f"m{i}": 1.0 for i in range(n)}


@pytest.mark.parametrize("over", [
    {"seq": -1},
    {"seq": MAX_EPOCHS},  # also what bounds a run to MAX_EPOCHS rows
    {"epoch": 0},
    {"epoch": 4},  # past epochs
    {"epochs": MAX_EPOCHS + 1},
    {"type": "heartbeat"},
    {"metrics": metrics(MAX_EVENT_METRICS + 1)},
    {"metrics": {"x" * 65: 1.0}},
    {"metrics": {"": 1.0}},
    {"metrics": {"loss": "high"}},
    {"metrics": {"loss": [1.0]}},
    {"metrics": [1.0]},
    {"extra": 1},
], ids=lambda o: str(o)[:40])
def test_a_bad_event_is_422(channel, client, over):
    assert post(client, {**EVENT, **over}).status_code == 422


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_a_metric_must_be_finite(channel, client, value):
    body = ('{"type": "progress", "seq": 0, "epoch": 1, "epochs": 3, "metrics": {"loss": %s}}' % value).encode()
    assert post(client, body).status_code == 422


def test_the_most_allowed_is_accepted(channel, client):
    assert post(client, {**EVENT, "seq": MAX_EPOCHS - 1, "epoch": MAX_EPOCHS, "epochs": MAX_EPOCHS, "metrics": metrics(MAX_EVENT_METRICS)}).status_code == 201


def test_a_big_body_is_413(channel, client):
    r = post(client, {**EVENT, "pad": "x" * MAX_EVENT_BODY_BYTES})
    assert r.status_code == 413


def test_the_token_hash_is_never_in_a_response(channel, client):
    channel.auth_row["token_hash"] = events_service.hash_token(TOKEN)
    for r in (post(client), post(client, headers={}), post(client, {**EVENT, "seq": -1})):
        assert channel.auth_row["token_hash"] not in r.text and TOKEN not in r.text


# ---- the token and the spec ----

@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(settings.training, "events_base_url", BASE_URL + "/")


def test_a_run_gets_a_token_whose_hash_is_stored_and_whose_secret_is_in_no_spec(monkeypatch, on):
    gw = Gateway(monkeypatch)
    conn = make_conn(monkeypatch)
    create()
    spec = gw.spec
    assert spec.reporting.token_env == EVENTS_TOKEN_ENV
    assert re.fullmatch(rf"{re.escape(BASE_URL)}/internal/v1/training-runs/[0-9a-f-]{{36}}/events", spec.reporting.events_url)
    assert gw.events_token and len(gw.events_token) >= 43  # token_urlsafe(32)
    (insert,) = [a for sql, a in conn.calls if sql.startswith("INSERT INTO training_runs")]
    assert insert[-1] == events_service.hash_token(gw.events_token)
    stored = json.dumps(insert[13])  # training_runs.spec
    assert gw.events_token not in stored and insert[-1] not in stored
    assert gw.events_token not in spec.model_dump_json()


def test_two_runs_get_different_tokens(monkeypatch, on):
    gw = Gateway(monkeypatch)
    make_conn(monkeypatch)
    create()
    first = gw.events_token
    create(key="k2")
    assert gw.events_token != first


def test_without_an_events_url_there_is_no_reporting_and_no_token(monkeypatch):
    assert settings.training.events_base_url == ""
    gw = Gateway(monkeypatch)
    conn = make_conn(monkeypatch)
    create()
    assert gw.spec.reporting is None and gw.events_token is None
    (insert,) = [a for sql, a in conn.calls if sql.startswith("INSERT INTO training_runs")]
    assert insert[-1] is None


def test_the_token_reaches_the_job_environment_not_the_spec(monkeypatch):
    from app.integrations import compute
    from app.integrations.data_plane import job_submit, train
    from app.integrations.job_spec import Reporting

    sent = {}

    async def submit(spec):
        sent["spec"] = spec
        return "id"

    async def noop(*a, **k):
        return "url"

    monkeypatch.setattr(compute, "submit", submit)
    monkeypatch.setattr(job_submit.secret_files, "get", lambda name: {"endpoint": "e", "access_key": "a", "secret_key": "s"})
    monkeypatch.setattr(train.access, "training_run", noop)
    monkeypatch.setattr(train.job_package, "presigned_url", noop)
    spec = SimpleNamespace(
        reporting=Reporting(events_url="http://x", token_env=EVENTS_TOKEN_ENV), job_id="j",
        resources=SimpleNamespace(max_runtime_s=1), model_dump_json=lambda: "{}")
    asyncio.run(train.submit_train("t", "p", "r", spec, "a" * 64, "the-secret"))
    job = sent["spec"]
    assert job.env_vars[EVENTS_TOKEN_ENV] == "the-secret"
    assert "the-secret" not in job.entrypoint


# ---- reading progress ----

def point(seq, **over):
    return {"seq": seq, "epoch": seq + 1, "epochs": 3, "metrics": {"train_loss": 1.0 / (seq + 1)}, "created_at": datetime.now(UTC), **over}


def get(monkeypatch, points, **row_over):
    row = run_row(**row_over)
    conn = FakeConn(row=row, rowset=points)
    monkeypatch.setattr(run_service, "TenantConn", lambda tenant_id: TenantCtx(conn))
    return asyncio.run(run_service.get_run(SimpleNamespace(tenant_id=str(TENANT)), PROJECT, row["id"]))


def test_get_shows_the_latest_event_and_the_epoch_history(monkeypatch):
    run = get(monkeypatch, [point(0), point(1)])
    assert [p.seq for p in run.progress.history] == [0, 1]
    assert run.progress.latest == run.progress.history[-1] and run.progress.latest.metrics == {"train_loss": 0.5}


def test_progress_is_kept_after_the_run_ends(monkeypatch):
    run = get(monkeypatch, [point(0), point(1), point(2)], status="succeeded")
    assert run.status == "succeeded" and run.progress.latest.epoch == 3 and len(run.progress.history) == 3


def test_a_run_with_no_events_has_no_progress(monkeypatch):
    assert get(monkeypatch, []).progress is None


def test_the_run_never_carries_the_token_hash(monkeypatch):
    run = get(monkeypatch, [point(0)], events_token_hash="h" * 64)
    assert "h" * 64 not in run.model_dump_json() and "events_token_hash" not in TrainingRun.model_fields


def test_the_progress_read_is_in_the_runs_tenant_transaction(monkeypatch):
    row = run_row()
    conn = FakeConn(row=row, rowset=[])
    monkeypatch.setattr(run_service, "TenantConn", lambda tenant_id: TenantCtx(conn))
    asyncio.run(run_service.get_run(SimpleNamespace(tenant_id=str(TENANT)), PROJECT, row["id"]))
    assert [sql for sql, _ in conn.calls if "training_run_events" in sql]  # on the same connection as the run, so under its RLS


# ---- the table ----

def test_the_table_is_tenant_isolated_and_cascades_from_the_run():
    sources = [(p.name, p.read_text()) for p in MIGRATIONS]
    table = schema_after(sources)["training_run_events"]
    assert table["scoped"] and table["rls"] and table["policies"]
    sql = next(p.read_text() for p in MIGRATIONS if p.name.endswith("_training_run_events.sql"))
    assert "REFERENCES training_runs(id) ON DELETE CASCADE" in sql
    assert "PRIMARY KEY (run_id, seq)" in sql
    assert "GRANT SELECT, INSERT ON training_run_events TO core_app" in sql


def test_the_events_type_check_equals_the_enum():
    sql = next(p.read_text() for p in MIGRATIONS if p.name.endswith("_training_run_events.sql"))
    allowed = re.search(r"training_run_events_type_check CHECK \(type IN \(([^)]*)\)\)", sql)[1]
    assert set(re.findall(r"'(\w+)'", allowed)) == {t.value for t in EventType}
