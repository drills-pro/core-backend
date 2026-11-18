"""POST /v1/predict from the console (planning/27 #8): a signed-in member with no API key, through the same route, limits and metering,
tagged source=console and stored, so the try-it log is a filtered list of predictions."""
import pytest

from app.core.authz import Action, CallSource, keys
from app.core.problem import Problem
from app.core.settings import settings
from tests.support.predict_env import BODY, MODEL, PROJECT, TENANT, build

USER = "user-1"


@pytest.fixture
def env(monkeypatch, dev_headers):
    state = build(monkeypatch)
    state.authorized = []
    state.denied = None

    async def authorize(action, tenant_id, actor, project_id, **kw):
        state.authorized.append((action, tenant_id, actor, project_id))
        if state.denied:
            raise state.denied
        return object()

    monkeypatch.setattr(keys, "authorize", authorize)

    def console(body=BODY, *, project=PROJECT, model=MODEL, tenant=TENANT, extra=None):
        headers = {"X-Tenant-Id": str(tenant), "X-User-Id": USER, "X-Project-Id": str(project), **({"X-Model-Id": str(model)} if model else {}), **(extra or {})}
        return state.client.post("/v1/predict", json=body, headers=headers)

    state.console = console
    return state


def test_a_member_without_a_key_gets_the_answer_and_the_call_is_stored_as_console(env):
    r = env.console()
    assert r.status_code == 200 and r.json()["output"] == {"predictions": [{"label": "cat"}]}
    assert env.authorized == [(Action.DEPLOYMENT_TRY, str(TENANT), USER, PROJECT)]  # the member needs the try-it action on the project
    (row,) = env.conn.preds.values()
    assert (row["source"], row["api_key_id"], row["project_id"], row["tenant_id"]) == (CallSource.CONSOLE, None, PROJECT, str(TENANT))
    assert row["status"] == "completed" and row["input"] == BODY["input"] and row["idempotency_key"] is None  # stored with no key to replay


def test_a_console_call_is_metered_like_any_call_once_per_prediction(env):
    body = env.console().json()
    (usage,) = env.usage
    assert usage["source_ref"] == body["id"].removeprefix("pred_") and usage["quantity"] == 1 and usage["project_id"] == PROJECT


def test_a_failed_console_call_is_stored_and_costs_nothing(env):
    from app.integrations.serve_client import ServeErrorCode
    from app.modules.user.deployments.predict import ServeFailed

    def fail(*a):
        raise ServeFailed(ServeErrorCode.BAD_INPUT, "bad", 1.0, None, 0)

    env.answer = fail
    assert env.console().status_code == 422
    (row,) = env.conn.preds.values()
    assert (row["source"], row["status"], row["error"]["code"]) == ("console", "failed", "model_rejected_input") and not env.usage


def test_the_plan_and_model_rate_limits_apply_to_the_console_too(env):
    env.limited = True
    r = env.console()
    assert (r.status_code, r.json()["code"]) == (429, "rate_limited") and not env.served and not env.conn.preds
    assert all(not k.startswith("key:") for k, _ in env.checks)  # there is no key whose own limit could apply


def test_a_member_who_may_not_try_it_is_refused_before_anything_is_read(env):
    env.denied = Problem(403, "forbidden", "missing action deployment.try", action=Action.DEPLOYMENT_TRY.value)
    r = env.console()
    assert r.status_code == 403 and r.json()["action"] == "deployment.try" and not env.served and not env.conn.calls


def test_the_project_header_must_be_a_project_uuid(env):
    assert env.console(project="nope").status_code == 404 and not env.authorized


def test_an_api_key_still_takes_the_key_path_and_is_tagged_api(env):
    assert env.post(key="k1").status_code == 200
    (row,) = env.conn.preds.values()
    assert (row["source"], row["api_key_id"] is not None) == (CallSource.API, True)


def test_without_dev_headers_a_call_with_no_credentials_is_the_one_invalid_key_401(env, monkeypatch):
    monkeypatch.setattr(settings.auth, "dev_headers", False)
    r = env.client.post("/v1/predict", json=BODY, headers={"X-Project-Id": str(PROJECT), "X-Model-Id": str(MODEL)})
    assert (r.status_code, r.json()["code"]) == (401, "invalid_api_key")


def test_the_prediction_sources_are_the_check_constraint():
    import re
    from pathlib import Path

    migration = next((Path(__file__).parents[4] / "migrations").glob("*_promote_deploys_console_predict.sql")).read_text()
    found = re.search(r"CONSTRAINT predictions_source_check CHECK \(source IN \(([^)]*)\)\)", migration)
    assert {m.value for m in CallSource} == set(re.findall(r"'([^']+)'", found.group(1)))

