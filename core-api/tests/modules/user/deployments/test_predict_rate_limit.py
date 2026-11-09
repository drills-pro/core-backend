"""POST /v1/predict rate limits (planning/24): the lower of the plan's and the key's limit, Retry-After from the fixed minute, and a
rejected call neither runs Serve nor takes an Idempotency-Key. rate_limit.check is faked; its window maths is tested here through Retry-After."""
import pytest

from app.core.enums import PlanTier
from app.modules.user.deployments import model_predict
from app.modules.user.deployments.constants import PLAN_TIER_RATE_LIMITS, RATE_LIMIT_WINDOW_SECONDS
from tests.support.predict_env import RESOLVED, TENANT, build

PLAN = PLAN_TIER_RATE_LIMITS[PlanTier.FREE]


@pytest.fixture
def env(monkeypatch):
    return build(monkeypatch)


def limits(env):
    return dict(env.checks)


def test_plan_and_key_limits_are_checked_under_their_own_keys(env):
    secret = env.add_key(rate_limit=PLAN + 5)
    assert env.post(secret=secret).status_code == 200
    (key_check, plan_check, deployment_check) = env.checks
    assert key_check[0].startswith("key:") and key_check[1] == PLAN + 5
    assert plan_check == (f"plan:{TENANT}", PLAN)
    assert deployment_check == (str(RESOLVED["id"]), RESOLVED["rate_limit_per_minute"])


def test_a_key_without_a_limit_is_held_to_the_plan_only(env):
    env.post()
    assert [k for k, _ in env.checks] == [f"plan:{TENANT}", str(RESOLVED["id"])]


def test_the_lower_limit_wins_when_the_key_is_lower(env):
    secret = env.add_key(rate_limit=2)
    first = env.post(secret=secret)
    env.limited_keys.add(env.checks[0][0])  # the key is now over its 2
    r = env.post(secret=secret)
    assert first.status_code == 200 and (r.status_code, r.json()["code"]) == (429, "rate_limited") and "this key" in r.json()["detail"]
    assert len(env.served) == 1


def test_the_lower_limit_wins_when_the_plan_is_lower(env):
    secret = env.add_key(rate_limit=PLAN * 10)
    env.limited_keys = {f"plan:{TENANT}"}
    r = env.post(secret=secret)
    assert (r.status_code, r.json()["code"]) == (429, "rate_limited") and "this plan" in r.json()["detail"] and not env.served
    assert limits(env)[f"plan:{TENANT}"] == PLAN  # the key's higher number never raises the plan's


def test_a_key_over_its_limit_does_not_use_up_the_plan_count(env):
    secret = env.add_key(rate_limit=1)
    env.limited = True
    env.post(secret=secret)
    assert [k.split(":")[0] for k, _ in env.checks] == ["key"]


def test_the_plan_tier_picks_the_plan_limit(env):
    env.plan_tier = PlanTier.PRO
    env.post()
    assert limits(env)[f"plan:{TENANT}"] == PLAN_TIER_RATE_LIMITS[PlanTier.PRO]


@pytest.mark.parametrize("now", [600.0, 629.4, 659.9])
def test_retry_after_is_the_seconds_left_in_the_fixed_minute(env, monkeypatch, now):
    monkeypatch.setattr(model_predict.time, "time", lambda: now)
    env.limited = True
    r = env.post()
    assert r.status_code == 429 and int(r.headers["Retry-After"]) == RATE_LIMIT_WINDOW_SECONDS - int(now) % RATE_LIMIT_WINDOW_SECONDS


@pytest.mark.parametrize("extra", [{"key": "k1"}, {"key": "k1", "extra": {"Prefer": "respond-async"}}])
def test_a_rejected_call_stores_nothing_and_does_not_take_the_idempotency_key(env, extra):
    secret = env.add_key(rate_limit=1)
    env.limited = True
    assert env.post(secret=secret, **extra).status_code == 429
    assert not env.conn.preds and not env.served and not env.usage
    env.limited = False
    r = env.post(secret=secret, **extra)  # the same key is still free
    assert r.status_code in (200, 202) and len(env.conn.preds) == 1


def test_a_background_submission_is_counted_once_at_accept(env):
    secret = env.add_key(rate_limit=5)
    assert env.post(secret=secret, key="k1", extra={"Prefer": "respond-async"}).status_code == 202
    assert [k.split(":")[0] for k, _ in env.checks].count("key") == 1
