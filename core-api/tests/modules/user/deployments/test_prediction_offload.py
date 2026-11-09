"""Prediction payloads over predict.inline_max_bytes live in the staging bucket (planning/24): the row keeps a ref, every read resolves it,
the attempt is in the output's key, and the retention purge deletes the objects. The bucket is a dict behind boto3's calls; the route, the
worker and the store's Python are real, the predictions table is in memory."""
import asyncio
import io
import uuid

import pytest
from botocore.exceptions import ClientError

from app.core.settings import settings
from app.integrations import serve_client
from app.integrations.data_plane import layout, prediction_payloads as payloads
from app.modules.user.deployments import prediction_store as store, prediction_worker as worker
from app.modules.user.deployments.constants import PredictionStatus
from tests.modules.user.deployments.test_predict_cancel_events import frames
from tests.support.predict_env import BODY, PROJECT, TENANT, build

ASYNC = {"Prefer": "respond-async"}
OK = (serve_client.Prediction(predictions=[{"label": "cat"}], cold_load=False), 7.5)
OUTPUT = {"predictions": [{"label": "cat"}]}
SMALL_LIMIT = 20  # bytes: BODY's input (27) and the output (35) are over it


class FakeBucket:
    """The staging bucket as boto3 sees it: put, get, list by prefix, delete."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.fail_prefix: str | None = None

    def put_object(self, *, Bucket, Key, Body, ContentType):
        assert Bucket == settings.data_plane.staging_bucket and ContentType == "application/json"
        self.objects[Key] = Body

    def get_object(self, *, Bucket, Key):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[Key])}

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return self

    def paginate(self, *, Bucket, Prefix):
        if self.fail_prefix and Prefix.startswith(self.fail_prefix):
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "ListObjectsV2")
        yield {"Contents": [{"Key": k} for k in self.objects if k.startswith(Prefix)]}

    def delete_objects(self, *, Bucket, Delete):
        for o in Delete["Objects"]:
            self.objects.pop(o["Key"], None)


@pytest.fixture
def env(monkeypatch):
    return build(monkeypatch)


@pytest.fixture
def bucket(monkeypatch):
    fake = FakeBucket()
    monkeypatch.setattr(payloads, "_s3", lambda: fake)
    return fake


@pytest.fixture
def small(monkeypatch):
    monkeypatch.setattr(settings.predict, "inline_max_bytes", SMALL_LIMIT)


def run(coro):
    return asyncio.run(coro)


def own_prefix(prediction_id) -> str:
    return layout.prediction_prefix(str(TENANT), str(PROJECT), str(prediction_id))


def test_the_default_limit_is_256_kb_and_a_payload_under_it_stays_inline(env, bucket):
    assert settings.predict.inline_max_bytes == 262144
    env.answer = lambda *a: OK
    assert env.post(key="k", extra=ASYNC).status_code == 202
    (row,) = env.conn.preds.values()
    run(worker.process_predictions())
    assert (row["input"], row["input_ref"], row["output"], row["output_ref"]) == (BODY["input"], None, OUTPUT, None) and not bucket.objects


def test_a_payload_exactly_at_the_limit_stays_inline_and_one_byte_over_goes_to_the_bucket(monkeypatch, bucket):
    monkeypatch.setattr(settings.predict, "inline_max_bytes", len('{"a": 1}'))
    assert run(payloads.store("t", "p", "i", "n.json", {"a": 1})).inline == {"a": 1} and not bucket.objects
    assert run(payloads.store("t", "p", "i", "n.json", {"a": 11})).ref == "predictions/t/p/i/n.json" and list(bucket.objects) == ["predictions/t/p/i/n.json"]


def test_a_big_background_prediction_round_trips_through_the_worker_get_events_and_the_replay(env, bucket, small):
    env.answer = lambda *a: OK
    accepted = env.post(key="k", extra=ASYNC)
    assert accepted.status_code == 202
    (row,) = env.conn.preds.values()
    input_key = f"{own_prefix(row['id'])}input.json"
    assert (row["input"], row["input_ref"]) == (None, input_key) and bucket.objects[input_key] == b'{"text": "hello", "n": 1.0}'

    run(worker.process_predictions())
    output_key = f"{own_prefix(row['id'])}1/output.json"
    assert (row["status"], row["output"], row["output_ref"]) == (PredictionStatus.COMPLETED, None, output_key) and output_key in bucket.objects
    (args,) = env.served
    assert args[4] == BODY["input"]  # the worker sent the input it read back from the bucket

    prediction_id = accepted.json()["id"]
    assert env.get(prediction_id).json()["output"] == OUTPUT
    events = frames(env.get(prediction_id, path="/events").text)
    assert [e for _, e, _ in events] == ["queued", "in_progress", "completed"] and events[2][2]["output"] == OUTPUT
    assert env.conn.events[-1][2]["output"] is None  # the events table never holds the big output
    replay = env.post(key="k", extra=ASYNC)
    assert (replay.status_code, replay.json()["output"]) == (200, OUTPUT)
    assert len(bucket.objects) == 2  # the replay's own input was not left behind


def test_a_big_sync_prediction_with_a_key_stores_both_objects_and_replays_the_output(env, bucket, small):
    env.answer = lambda *a: OK
    first = env.post(key="k")
    (row,) = env.conn.preds.values()
    assert (first.status_code, first.json()["output"]) == (200, OUTPUT)
    assert (row["input_ref"], row["output_ref"]) == (f"{own_prefix(row['id'])}input.json", f"{own_prefix(row['id'])}1/output.json")
    assert (row["input"], row["output"]) == (None, None)
    again = env.post(key="k")
    assert (again.status_code, again.json()["output"], len(env.served)) == (200, OUTPUT, 1) and len(bucket.objects) == 2


def test_the_output_of_a_stale_attempt_goes_beside_the_current_attempts_never_over_it(env, bucket, small):
    env.post(key="k", extra=ASYNC)
    (row,) = env.conn.preds.values()
    row.update(attempt=1, status=PredictionStatus.IN_PROGRESS)
    stale = {**row}
    first = run(store.stash_output(stale, {"v": "stale attempt output"}))
    row.update(attempt=2)
    current = run(store.stash_output(row, {"v": "current attempt output"}))
    assert (first.ref, current.ref) == (f"{own_prefix(row['id'])}1/output.json", f"{own_prefix(row['id'])}2/output.json")
    assert bucket.objects[first.ref] == b'{"v": "stale attempt output"}' and bucket.objects[current.ref] == b'{"v": "current attempt output"}'
    assert run(store.finish(env.conn, stale, PredictionStatus.COMPLETED, output_ref=first.ref)) is None  # the fence drops the stale write-back
    moved = run(store.finish(env.conn, row, PredictionStatus.COMPLETED, output_ref=current.ref))
    assert moved["output_ref"] == current.ref and run(store.payload(moved, "output")) == {"v": "current attempt output"}


def test_a_ref_outside_the_rows_tenant_and_project_is_refused(bucket):
    prediction, other = uuid.uuid4(), str(uuid.uuid4())
    refs = (
        f"{layout.prediction_prefix(other, str(PROJECT), str(prediction))}input.json",  # another tenant's
        f"{layout.prediction_prefix(str(TENANT), other, str(prediction))}input.json",  # another project's
        f"{own_prefix(uuid.uuid4())}input.json",  # another prediction's
        f"{own_prefix(prediction)}../../../{other}/x/y/input.json",  # a way out of its own prefix
        "imports/anything",
    )
    for ref in refs:
        bucket.objects[ref] = b"1"
        row = {"tenant_id": TENANT, "project_id": PROJECT, "id": prediction, "input": None, "input_ref": ref}
        with pytest.raises(payloads.PayloadRefError):
            run(store.payload(row, "input"))


def test_a_forged_input_ref_is_never_read_and_the_attempt_is_retried(env, bucket, small):
    env.answer = lambda *a: OK
    env.post(key="k", extra=ASYNC)
    (row,) = env.conn.preds.values()
    other = f"predictions/{uuid.uuid4()}/{PROJECT}/{row['id']}/input.json"
    bucket.objects[other] = b'{"secret": 1}'
    row["input_ref"] = other
    run(worker.process_predictions())
    assert not env.served and row["status"] == PredictionStatus.QUEUED


class Pool:
    """The pool the purge poller uses: the expiring refs it lists and the ids it passes back to purge_predictions."""

    def __init__(self, expiring):
        self.expiring, self.cleaned = expiring, None

    def acquire(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def fetch(self, sql):
        assert sql == worker._EXPIRING_REFS
        return self.expiring

    async def fetchval(self, sql, cleaned):
        assert sql == worker._PURGE
        self.cleaned = cleaned
        return len(cleaned)


def expiring(*ids):
    return [{"id": i, "tenant_id": TENANT, "project_id": PROJECT} for i in ids]


def with_pool(monkeypatch, pool):
    async def get_pool():
        return pool

    monkeypatch.setattr(worker, "get_pool", get_pool)


def test_the_purge_deletes_every_object_of_the_prediction_stale_attempts_included_and_tolerates_a_missing_one(monkeypatch, bucket):
    here, gone, other = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    for name in ("input.json", "1/output.json", "2/output.json"):
        bucket.objects[f"{own_prefix(here)}{name}"] = b"{}"
    kept = f"{own_prefix(other)}input.json"
    bucket.objects[kept] = b"{}"
    pool = Pool(expiring(here, gone))  # `gone` has a ref in its row but no object any more
    with_pool(monkeypatch, pool)
    run(worker.purge_stored_predictions())
    assert list(bucket.objects) == [kept] and pool.cleaned == [here, gone]


def test_a_failed_delete_keeps_the_row_for_the_next_run(monkeypatch, bucket, caplog):
    ok, broken = uuid.uuid4(), uuid.uuid4()
    bucket.fail_prefix = own_prefix(broken)
    pool = Pool(expiring(broken, ok))
    with_pool(monkeypatch, pool)
    run(worker.purge_stored_predictions())
    assert pool.cleaned == [ok] and str(broken) in caplog.text


def test_the_purge_with_nothing_expiring_still_runs_the_sql_with_no_ids(monkeypatch, bucket):
    pool = Pool([])
    with_pool(monkeypatch, pool)
    run(worker.purge_stored_predictions())
    assert pool.cleaned == []


def test_a_missing_object_of_a_live_prediction_is_an_error_not_a_null(bucket):
    prediction = uuid.uuid4()
    row = {"tenant_id": TENANT, "project_id": PROJECT, "id": prediction, "output": None, "output_ref": f"{own_prefix(prediction)}1/output.json"}
    with pytest.raises(payloads.PayloadMissing):
        run(store.payload(row, "output"))
