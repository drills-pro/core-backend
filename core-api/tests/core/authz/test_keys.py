"""API key generation and require_key(): the 401/403 table through a real FastAPI app, database faked."""
import uuid

import pytest
from fastapi import Depends

from app.core.authz import KeyContext, Scope, TenantStatus, generate_key, keys, require_key
from tests.support import make_client

TENANT, PROJECT, MODEL, OTHER_MODEL = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def test_generated_key_has_prefix_and_only_its_hash_is_derivable():
    a, b = generate_key(), generate_key()
    assert a.secret.startswith("drl_") and a.secret != b.secret
    assert a.prefix == a.secret[:12] and a.secret not in (a.prefix, a.key_hash)
    assert a.key_hash == keys.hash_key(a.secret) and len(a.key_hash) == 64
    assert a.secret[12:] not in a.key_hash


class FakePool:
    """api_key_lookup returns the row for a known hash; revoked and expired keys return none, as the SQL does."""

    def __init__(self):
        self.rows = {}
        self.touched = []

    def add(self, scopes=("predict",), models=(), revoked=False, expired=False, tenant_status=TenantStatus.ACTIVE):
        key = generate_key()
        if not (revoked or expired):
            self.rows[key.key_hash] = dict(
                key_id=uuid.uuid4(), tenant_id=TENANT, project_id=PROJECT, scopes=list(scopes), model_ids=list(models),
                tenant_status=tenant_status, rate_limit_per_minute=None,
            )
        return key.secret

    def acquire(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def fetchrow(self, sql, key_hash):
        return self.rows.get(key_hash)

    async def execute(self, sql, key_id):
        self.touched.append(key_id)


@pytest.fixture
def pool(monkeypatch):
    pool = FakePool()

    async def get_pool():
        return pool

    monkeypatch.setattr(keys, "get_pool", get_pool)
    return pool


@pytest.fixture
def client(pool):
    http = make_client(headers=None)
    app = http.app

    @app.post("/v1/predict")
    async def predict(ctx: KeyContext = Depends(require_key(Scope.PREDICT))):
        return {"project_id": str(ctx.project_id), "tenant_id": ctx.tenant_id, "scopes": sorted(ctx.scopes)}

    return http


def call(client, secret, project=str(PROJECT), model=None):
    headers = {}
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    if project:
        headers["X-Project-Id"] = project
    if model:
        headers["X-Model-Id"] = str(model)
    return client.post("/v1/predict", headers=headers)


def test_valid_key_resolves_tenant_and_project_from_the_key(client, pool):
    r = call(client, pool.add())
    assert (r.status_code, r.json()) == (200, {"project_id": str(PROJECT), "tenant_id": str(TENANT), "scopes": ["predict"]})


def test_only_an_accepted_request_marks_the_key_used(client, pool):
    good = pool.add()
    assert call(client, good).status_code == 200
    assert call(client, pool.add(scopes=())).status_code == 403
    assert call(client, "drl_unknown").status_code == 401
    assert len(pool.touched) == 1 and pool.touched[0] == pool.rows[keys.hash_key(good)]["key_id"]


def test_every_authentication_failure_is_the_same_401(client, pool):
    good = pool.add()
    bodies = []
    for secret, project in [
        (None, str(PROJECT)),  # no key
        ("not-a-drl-key", str(PROJECT)),
        (generate_key().secret, str(PROJECT)),  # unknown
        (pool.add(revoked=True), str(PROJECT)),
        (pool.add(expired=True), str(PROJECT)),
        (good, str(uuid.uuid4())),  # another project
        (good, None),  # header missing
    ]:
        r = call(client, secret, project)
        assert (r.status_code, r.json()["code"]) == (401, "invalid_api_key")
        assert r.headers["content-type"] == "application/problem+json"
        body = r.json()
        assert body.pop("request_id")
        bodies.append(body)
    assert all(b == bodies[0] for b in bodies)


@pytest.mark.parametrize("tenant_status", [TenantStatus.SUSPENDED, TenantStatus.PENDING_DELETE])
def test_a_key_of_a_tenant_that_is_not_active_is_503_tenant_suspended(client, pool, tenant_status):
    r = call(client, pool.add(tenant_status=tenant_status))
    assert (r.status_code, r.json()["code"]) == (503, "tenant_suspended")


def test_missing_scope_is_403_naming_the_scope(client, pool):
    r = call(client, pool.add(scopes=()))
    body = r.json()
    assert (r.status_code, body["code"], body["scope"]) == (403, "forbidden", "predict")
    assert "predict" in body["detail"]


def test_the_key_dependency_accepts_any_model_header_the_route_checks_the_allowlist(client, pool):
    # The allowlist is checked by the predict route once it knows the deployment's model (test_model_predict), so a
    # deployment id in X-Model-Id is not refused here.
    limited = pool.add(models=[MODEL])
    assert call(client, limited, model=OTHER_MODEL).status_code == 200
    assert call(client, limited, model="not-a-uuid").status_code == 200
    assert call(client, limited).status_code == 200


def test_key_context_allows_model():
    ctx = KeyContext(uuid.uuid4(), str(TENANT), PROJECT, frozenset({Scope.PREDICT}), frozenset({MODEL}))
    assert ctx.allows_model(str(MODEL)) and not ctx.allows_model(str(OTHER_MODEL)) and not ctx.allows_model(None)
