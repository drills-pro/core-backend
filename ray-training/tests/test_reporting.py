"""Progress events: sent only when the spec has `reporting`, one per epoch with seq = epoch index, and never able to stop training."""
import json
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import jsonschema
import pytest

from ray_core import reporting
from ray_core.constants import EVENTS_TIMEOUT_SECONDS
from ray_core.spec import parse_spec
from tests.support import classification_rows, spec_doc
from tests.test_spec import CONTRACT

ROWS = classification_rows()[0]
URL, ENV, TOKEN = "http://core-api.test/internal/v1/training-runs/r/events", "TRAINING_EVENTS_TOKEN", "s3cret"
EPOCH = {"epoch": 2, "train_loss": 0.25, "accuracy": 0.5}


def spec(with_reporting=True):
    doc = spec_doc("image-classification", "m", ROWS)
    doc["payload"]["hyperparameters"]["epochs"] = 3
    if with_reporting:
        doc["reporting"] = {"events_url": URL, "token_env": ENV}
    return parse_spec(json.dumps(doc))


@pytest.fixture
def sent(monkeypatch):
    calls = []

    def urlopen(request, timeout):
        calls.append((request, timeout))
        return SimpleNamespace(close=lambda: None)

    monkeypatch.setattr(reporting, "urlopen", urlopen)
    monkeypatch.setenv(ENV, TOKEN)
    return calls


def test_the_spec_names_where_to_post_and_which_variable_holds_the_token():
    s = spec()
    assert (s.events_url, s.events_token_env) == (URL, ENV)
    assert TOKEN not in json.dumps(vars(s), default=str)
    jsonschema.validate(json.loads(json.dumps({**spec_doc("image-classification", "m", ROWS), "reporting": {"events_url": URL, "token_env": ENV}})), json.loads(CONTRACT.read_text()))


def test_a_spec_without_reporting_sends_nothing(sent):
    s = spec(with_reporting=False)
    assert (s.events_url, s.events_token_env) == (None, None)
    reporting.post_progress(s, EPOCH)
    assert sent == []


def test_an_epoch_is_posted_with_the_token_and_seq_is_the_epoch_index(sent):
    reporting.post_progress(spec(), EPOCH)
    ((request, timeout),) = sent
    assert (request.full_url, request.get_method(), timeout) == (URL, "POST", EVENTS_TIMEOUT_SECONDS)
    assert request.get_header("Authorization") == f"Bearer {TOKEN}"
    assert json.loads(request.data) == {"type": "progress", "seq": 1, "epoch": 2, "epochs": 3, "metrics": {"train_loss": 0.25, "accuracy": 0.5}}


def test_only_finite_numbers_are_sent_as_metrics(sent):
    reporting.post_progress(spec(), {**EPOCH, "loss": float("nan"), "lr": float("inf"), "flag": True, "labels": ["a"], "n": 3})
    assert json.loads(sent[0][0].data)["metrics"] == {"train_loss": 0.25, "accuracy": 0.5, "n": 3.0}


@pytest.mark.parametrize("failure", [
    URLError("connection refused"), TimeoutError(), HTTPError(URL, 409, "not running", {}, None), OSError("down"), ValueError("bad"),
], ids=lambda e: type(e).__name__)
def test_a_failed_post_is_logged_and_swallowed(monkeypatch, caplog, failure):
    def urlopen(request, timeout):
        raise failure

    monkeypatch.setattr(reporting, "urlopen", urlopen)
    monkeypatch.setenv(ENV, TOKEN)
    reporting.post_progress(spec(), EPOCH)  # does not raise
    assert "not delivered" in caplog.text and TOKEN not in caplog.text


def test_a_missing_token_variable_or_an_epochless_entry_is_swallowed_too(monkeypatch, sent, caplog):
    monkeypatch.delenv(ENV)
    reporting.post_progress(spec(), EPOCH)
    monkeypatch.setenv(ENV, TOKEN)
    reporting.post_progress(spec(), {})  # the history entry of a rank that evaluated nothing
    assert sent == [] and caplog.text.count("not delivered") == 2


def test_the_ray_session_posts_after_the_checkpoint_and_a_failing_post_does_not_fail_the_epoch(monkeypatch):
    worker = pytest.importorskip("ray_core.worker")
    order = []
    monkeypatch.setattr(worker.ray.train, "get_context", lambda: SimpleNamespace(get_world_rank=lambda: 0, get_world_size=lambda: 1))
    monkeypatch.setattr(worker.ray.train, "report", lambda *a, **k: order.append("checkpoint"))
    monkeypatch.setattr(worker.reporting, "urlopen", lambda *a, **k: (order.append("post"), (_ for _ in ()).throw(OSError("down")))[1])
    monkeypatch.setenv(ENV, TOKEN)
    session = worker.RaySession(spec())
    session.report(EPOCH, lambda: {"epoch": 2})
    assert order == ["checkpoint", "post"]

    order.clear()
    worker.RaySession(spec(with_reporting=False)).report(EPOCH, lambda: {"epoch": 2})  # nothing is sent
    assert order == ["checkpoint"]
