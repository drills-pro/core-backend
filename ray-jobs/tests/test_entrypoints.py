"""Each job type end to end through the shared runner, with a fake store; parquet is swapped for JSON (conftest)."""
import json
import signal

import pytest

from ray_jobs import runner
from ray_jobs.common.constants import (
    IMAGE_CLASSIFICATION, NO_ALARM, OBJECT_DETECTION, REPORT_KEY, TABULAR_CLASSIFICATION)
from ray_jobs.common.enums import Code, Outcome
from ray_jobs.common.report import Report
from ray_jobs.common.rows import image_row
from ray_jobs.entrypoints import huggingface, upload_csv, upload_images
from tests.support import FakeStore, JPG, LIMITS, PNG, codes, fake_decode, params, zip_bytes


def final_report(store) -> dict:
    return json.loads(store.puts[REPORT_KEY])


def run_images(p, store):
    return runner.run(p, store, upload_images.extract, fake_decode)


def run_csv(p, store):
    return runner.run(p, store, upload_csv.extract)


# ---- upload_images ----

def test_a_bad_zip_writes_a_failed_report_and_no_output():
    store = FakeStore(b"not a zip")
    rep = run_images(params(IMAGE_CLASSIFICATION), store)
    assert rep.failed and set(store.puts) == {REPORT_KEY} and final_report(store)["outcome"] == Outcome.FAILED


def test_an_unsafe_archive_stops_before_writing():
    store = FakeStore(zip_bytes({"../x.jpg": JPG}))
    rep = run_images(params(IMAGE_CLASSIFICATION), store)
    assert codes(rep) == [Code.ARCHIVE_UNSAFE_PATH] and set(store.puts) == {REPORT_KEY}


def test_classification_success_writes_objects_index_and_ok_report(fake_parquet):
    store = FakeStore(zip_bytes({"cat/1.jpg": JPG, "dog/2.png": PNG}))
    rep = run_images(params(IMAGE_CLASSIFICATION, split="train"), store)
    assert not rep.failed
    assert set(store.puts) == {"out/objects/cat/1.jpg", "out/objects/dog/2.png", "out/index.parquet", REPORT_KEY}
    index = json.loads(store.puts["out/index.parquet"])
    assert [(r["path"], r["label"], r["split"]) for r in index] == [("cat/1.jpg", "cat", "train"), ("dog/2.png", "dog", "train")]
    final = final_report(store)
    assert final["outcome"] == "ok" and final["stats"]["row_count"] == 2 and final["stats"]["classes"] == {"cat": 1, "dog": 1}


def test_detection_zip_is_routed_to_the_coco_reader(fake_parquet):
    doc = {"images": [{"id": 1, "file_name": "1.jpg"}], "categories": [{"id": 1, "name": "cat"}],
           "annotations": [{"id": 1, "image_id": 1, "category_id": 1, "bbox": [1, 2, 3, 4]}]}
    store = FakeStore(zip_bytes({"annotations.json": json.dumps(doc).encode(), "1.jpg": JPG}))
    assert not run_images(params(OBJECT_DETECTION), store).failed
    assert json.loads(store.puts["out/index.parquet"])[0]["boxes"][0]["category"] == "cat"


def test_merges_with_the_base_index_and_skips_duplicates(fake_parquet, monkeypatch):
    base = [image_row("cat/1.jpg", JPG, "cat", None, None), image_row("bird/9.jpg", JPG, "bird", None, None)]
    monkeypatch.setattr("ray_jobs.merge.http_get", lambda url: json.dumps(base).encode())
    store = FakeStore(zip_bytes({"cat/1.jpg": JPG, "dog/2.png": PNG}))
    rep = run_images(params(IMAGE_CLASSIFICATION, base={"index_url": "http://base/index"}), store)
    assert not rep.failed
    assert [r["path"] for r in json.loads(store.puts["out/index.parquet"])] == ["cat/1.jpg", "bird/9.jpg", "dog/2.png"]
    # additive staging: out/ carries the merged index and the new objects only; the base's objects stay in lakeFS
    assert "out/objects/bird/9.jpg" not in store.puts and "out/objects/dog/2.png" in store.puts
    assert final_report(store)["stats"]["duplicates_skipped"] == 1


def test_base_conflict_writes_no_index(fake_parquet, monkeypatch):
    monkeypatch.setattr("ray_jobs.merge.http_get", lambda url: json.dumps([image_row("cat/1.jpg", PNG, "cat", None, None)]).encode())
    store = FakeStore(zip_bytes({"cat/1.jpg": JPG}))
    rep = run_images(params(IMAGE_CLASSIFICATION, base={"index_url": "http://base/index"}), store)
    assert codes(rep) == [Code.MERGE_CONFLICT] and "out/index.parquet" not in store.puts
    assert final_report(store)["outcome"] == "failed"


def test_a_timeout_is_reported():
    store = FakeStore(zip_bytes({"cat/1.jpg": JPG}))

    def slow(_data):
        raise TimeoutError

    rep = runner.run(params(IMAGE_CLASSIFICATION), store, upload_images.extract, slow)
    assert codes(rep) == [Code.JOB_TIMEOUT] and final_report(store)["outcome"] == "failed"


# ---- upload_csv ----

def test_an_oversized_upload_is_refused_before_it_is_downloaded():
    store = FakeStore(b"x" * (LIMITS.upload_max_bytes + 1))
    store.download = lambda *_: pytest.fail("downloaded")
    rep = run_csv(params(TABULAR_CLASSIFICATION, target="y"), store)
    assert codes(rep) == [Code.ARCHIVE_TOO_LARGE] and set(store.puts) == {REPORT_KEY}


def test_an_internal_error_still_writes_the_report():
    store = FakeStore(b"")
    store.download = lambda *_: (_ for _ in ()).throw(RuntimeError("boom"))
    rep = run_csv(params(TABULAR_CLASSIFICATION, target="y"), store)
    assert codes(rep) == [Code.INTERNAL_ERROR] and REPORT_KEY in store.puts


def test_bad_limits_still_write_a_report():
    store = FakeStore(b"")
    rep = run_csv({"source": "upload", "task": TABULAR_CLASSIFICATION, "limits": {}}, store)
    assert codes(rep) == [Code.INTERNAL_ERROR] and final_report(store)["outcome"] == "failed"


def test_csv_success_and_merge_with_a_base(fake_parquet, monkeypatch):
    store = FakeStore(b"a,b,y\n1,,x\n2,,z\n")
    rep = run_csv(params(TABULAR_CLASSIFICATION, target="y", split="test"), store)
    assert not rep.failed and set(store.puts) == {"out/data.parquet", "out/schema.json", REPORT_KEY}
    schema = json.loads(store.puts["out/schema.json"])
    assert schema == {"columns": [{"name": "a", "type": "int64"}, {"name": "b", "type": "string"}, {"name": "y", "type": "string"}]}
    assert json.loads(store.puts["out/data.parquet"])[0] == {"a": 1, "b": None, "y": "x", "split": "test"}
    assert final_report(store)["stats"]["classes"] == {"x": 1, "z": 1}

    base = {"data_url": "http://b/data", "schema_url": "http://b/schema"}
    base_rows = [{"a": 0, "b": None, "y": "q", "split": None}]
    monkeypatch.setattr("ray_jobs.merge.http_get", lambda url: json.dumps(schema if url.endswith("schema") else base_rows).encode())
    store = FakeStore(b"a,b,y\n1,,x\n2,,z\n")
    rep = run_csv(params(TABULAR_CLASSIFICATION, target="y", base=base), store)
    assert not rep.failed and len(json.loads(store.puts["out/data.parquet"])) == 3
    store = FakeStore(b"a,b,y\n1.5,,x\n2,,z\n")  # a is now float64: schema differs from the base
    rep = run_csv(params(TABULAR_CLASSIFICATION, target="y", base=base), store)
    assert codes(rep) == [Code.SCHEMA_MISMATCH] and "out/data.parquet" not in store.puts


# ---- huggingface ----

HF_INFO = {"sha": "c0ffee", "cardData": {"license": "mit", "task_categories": ["tabular-classification"],
                                         "dataset_info": {"features": [{"name": "age", "dtype": "int64"},
                                                                       {"name": "churn", "dtype": "bool"}]}},
           "siblings": [{"rfilename": "train.jsonl", "size": 10}, {"rfilename": "loader.py", "size": 1}]}


def run_hf(p, store):
    return runner.run(p, store, huggingface.extract, fake_decode)


def test_hf_tabular_success_pins_revision_and_keeps_split(fake_parquet, monkeypatch):
    fetched = []
    monkeypatch.setattr("ray_jobs.sources.huggingface.hf_json", lambda repo, revision: HF_INFO)

    def fake_get(url, max_bytes=None):
        fetched.append(url)
        return b'{"age": 30, "churn": true}\n{"age": 41, "churn": false}\n'

    monkeypatch.setattr("ray_jobs.sources.huggingface.http_get", fake_get)
    store = FakeStore()
    rep = run_hf(params(TABULAR_CLASSIFICATION, source="huggingface", hf={"repo": "o/r", "revision": "c0ffee"}), store)
    assert not rep.failed and fetched == ["https://huggingface.co/datasets/o/r/resolve/c0ffee/train.jsonl"]
    assert [r["split"] for r in json.loads(store.puts["out/data.parquet"])] == ["train", "train"]
    final = final_report(store)
    assert (final["revision"], final["license"], final["stats"]["classes"]) == ("c0ffee", ["mit"], {"True": 1, "False": 1})


def test_hf_image_success_and_card_mismatch(fake_parquet, monkeypatch):
    from tests.test_huggingface import FEATURES

    info = {"sha": "abc", "cardData": {"task_categories": ["image-classification"], "dataset_info": {"features": FEATURES}},
            "siblings": [{"rfilename": "test-0.parquet", "size": 5}]}
    monkeypatch.setattr("ray_jobs.sources.huggingface.hf_json", lambda repo, revision: info)
    monkeypatch.setattr("ray_jobs.sources.huggingface.http_get", lambda url, max_bytes=None: b"")
    monkeypatch.setattr("ray_jobs.sources.huggingface.read_parquet", lambda data, max_bytes=None: [{"image": {"bytes": JPG}, "label": 1}])
    store = FakeStore()
    rep = run_hf(params(IMAGE_CLASSIFICATION, source="huggingface", hf={"repo": "o/r", "revision": "abc"}), store)
    assert not rep.failed
    (row,) = json.loads(store.puts["out/index.parquet"])
    assert row["label"] == "dog" and row["split"] == "test" and f"out/objects/{row['path']}" in store.puts
    store = FakeStore()
    rep = run_hf(params(TABULAR_CLASSIFICATION, source="huggingface", target="x", hf={"repo": "o/r", "revision": "abc"}), store)
    assert Code.HF_CARD_MISMATCH in codes(rep) and not any(k.startswith("out/") for k in store.puts)


# ---- the shared runner ----

def test_a_card_license_is_always_a_list():
    from ray_jobs.sources.huggingface import license_list
    assert license_list(["cc-by-4.0", "mit"]) == ["cc-by-4.0", "mit"] and license_list("mit") == ["mit"] and license_list(None) is None


def test_the_alarm_is_cancelled_before_the_report_is_written(monkeypatch):
    calls = []
    monkeypatch.setattr(runner.signal, "alarm", lambda n: calls.append(("alarm", n)))
    store = FakeStore(b"not a zip")
    store.put = lambda key, data: calls.append(("put", key))
    run_images(params(IMAGE_CLASSIFICATION), store)
    assert calls == [("alarm", NO_ALARM), ("put", REPORT_KEY)]


@pytest.mark.parametrize("module", [upload_images, upload_csv, huggingface])
def test_main_arms_the_alarm_and_maps_failure_to_exit_code(monkeypatch, module):
    alarms, handlers = [], {}
    monkeypatch.setattr(runner.signal, "alarm", alarms.append)
    monkeypatch.setattr(runner.signal, "signal", lambda sig, fn: handlers.update({sig: fn}))
    for var in ("STAGING_ENDPOINT", "STAGING_ACCESS_KEY", "STAGING_SECRET_KEY"):
        monkeypatch.setenv(var, "x")
    monkeypatch.setattr(runner, "S3Store", lambda *a: FakeStore())
    for failed, code in ((True, 1), (False, 0)):
        rep = Report()
        if failed:
            rep.error(Code.NO_DATA, "x")
        monkeypatch.setattr(runner, "run", lambda params, store, extract, rep=rep: rep)
        payload = {"bucket": "b", "prefix": "p/", **params(IMAGE_CLASSIFICATION)}
        argv = ["x", json.dumps({"spec_version": 1, "kind": "ingest", "payload": payload})]
        assert runner.main(argv, module.extract) == code
    assert alarms == [LIMITS.job_timeout_seconds, NO_ALARM] * 2  # armed, then cancelled after run()
    with pytest.raises(TimeoutError):
        handlers[signal.SIGALRM]()
