"""The batch runner against a tiny ONNX model: results equal live ones, a bad item never ends the job, report.json is always written.
Needs onnx, onnxruntime, numpy, pyarrow and PIL; the Ray Data path runs only where ray.data imports (it needs pandas)."""
import base64
import io
import json
import uuid

import pytest

pytest.importorskip("onnxruntime")
pytest.importorskip("onnx")
pytest.importorskip("PIL")
pytest.importorskip("pyarrow")

import pyarrow as pa
import pyarrow.parquet as pq

from ray_serving import model
from ray_serving.batch_spec import allowed_base, parse_spec
from ray_serving.constants import MAX_REPORT_FAILURES, PREDICTIONS_KEY, REPORT_KEY
from ray_serving.entrypoints import predict_batch, smoke
from ray_serving.store import DirStore


def png(color) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def world(tmp_path):
    """A model, three good images, one corrupt, one the manifest does not know, one whose file is gone, and the payload for them."""
    art = tmp_path / "model"
    art.mkdir()
    smoke.tiny_model(art / "model.onnx")
    (art / "model.json").write_text(json.dumps({"preprocessing": {
        "resize": [smoke.SIZE, smoke.SIZE], "resample": "bilinear", "rescale": 1 / 255, "mean": [0.5] * 3, "std": [0.25] * 3}}))
    imgs = tmp_path / "imgs"
    imgs.mkdir()
    files = {"a/1.png": png((200, 20, 20)), "a/2.png": png((20, 200, 20)), "b/3.png": png((20, 20, 200)), "b/bad.png": b"not an image"}
    manifest = {}
    for path, data in files.items():
        (imgs / path.replace("/", "_")).write_bytes(data)
        manifest[path] = (imgs / path.replace("/", "_")).as_uri()
    manifest["c/gone.png"] = (imgs / "gone").as_uri()  # listed and signed, but the object is missing
    index = tmp_path / "index.parquet"
    pq.write_table(pa.table({"path": [*files, "c/gone.png", "d/unsigned.png"]}), index)
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    spec = {"model_url": (art / "model.onnx").as_uri(), "manifest_url": (art / "model.json").as_uri(), "tokenizer_url": "",
            "class_names": smoke.CLASSES, "params": {}, "providers": ["CPUExecutionProvider"], "encoder": smoke.ENCODER, "postprocess": smoke.POSTPROCESS}
    payload = {"bucket": "b", "prefix": "batch/", "concurrency": 2, "batch_size": 2, "model": spec,
               "data": {"index_url": index.as_uri(), "manifest_url": (tmp_path / "manifest.json").as_uri(), "version_id": str(uuid.uuid4()), "lakefs_commit": "c0ffee"}}
    return payload, files, tmp_path


def local_predict(payload, items):
    """The Ray step without Ray: one model, the same predict_items the actors call."""
    spec = payload["model"]
    loaded = model.load(model.ModelSpec.from_dict(spec), allowed_base(spec["model_url"]))
    return predict_batch.predict_items(loaded, items)


def lines(store_root, key):
    return [json.loads(line) for line in (store_root / "batch" / key).read_text().splitlines()]


def test_a_batch_result_equals_a_live_one_and_a_bad_item_only_fails_itself(world):
    payload, files, root = world
    report = predict_batch.run(payload, DirStore(str(root / "out"), "batch/"), predict=local_predict)
    rows = {r["path"]: r for r in lines(root / "out", PREDICTIONS_KEY)}
    spec = payload["model"]
    live = model.load(model.ModelSpec.from_dict(spec), allowed_base(spec["model_url"]))
    for path in ("a/1.png", "a/2.png", "b/3.png"):
        assert rows[path]["ok"] and rows[path]["predictions"] == model.predict(live, {"image_base64": base64.b64encode(files[path]).decode()})
    assert {p: rows[p]["code"] for p in ("b/bad.png", "c/gone.png", "d/unsigned.png")} == {
        "b/bad.png": model.ErrorCode.BAD_INPUT, "c/gone.png": predict_batch.ItemCode.FETCH_FAILED, "d/unsigned.png": predict_batch.ItemCode.MISSING_OBJECT}
    written = json.loads((root / "out" / "batch" / REPORT_KEY).read_text())
    assert written == json.loads(json.dumps(report))
    assert (report["status"], report["counts"]) == ("succeeded", {"total": 6, "succeeded": 3, "failed": 3})
    assert sorted(f["path"] for f in report["failures"]) == ["b/bad.png", "c/gone.png", "d/unsigned.png"]
    assert report["dataset"] == {"version_id": payload["data"]["version_id"], "lakefs_commit": "c0ffee"}


def test_the_actor_answers_a_ray_data_batch_the_way_predict_items_does(world):
    payload, _, _ = world
    items = predict_batch.list_items(payload["data"])
    actor = predict_batch.BatchPredictor(payload["model"], allowed_base(payload["model"]["model_url"]))
    out = actor({"path": [i["path"] for i in items], "url": [i["url"] for i in items]})
    expected = local_predict(payload, items)
    assert out["path"] == [r["path"] for r in expected] and out["ok"] == [r["ok"] for r in expected]
    assert [json.loads(p) for p in out["predictions"]] == [r["predictions"] for r in expected]
    assert [c or None for c in out["code"]] == [r["code"] for r in expected]


def test_report_json_is_written_when_the_job_itself_fails(world, tmp_path):
    payload, _, _ = world
    store = DirStore(str(tmp_path / "out"), "batch/")
    payload["data"]["index_url"] = (tmp_path / "nope.parquet").as_uri()
    report = predict_batch.run(payload, store)
    assert (report["status"], report["code"]) == ("failed", predict_batch.ItemCode.INTERNAL)
    assert json.loads((tmp_path / "out" / "batch" / REPORT_KEY).read_text())["status"] == "failed"
    assert not (tmp_path / "out" / "batch" / PREDICTIONS_KEY).exists()


def test_a_ray_step_that_raises_still_leaves_a_report(world, tmp_path):
    payload, _, _ = world

    def boom(*_):
        raise MemoryError("actor died")

    report = predict_batch.run(payload, DirStore(str(tmp_path / "out"), "batch/"), predict=boom)
    assert report["status"] == "failed" and "actor died" in report["message"]
    assert (tmp_path / "out" / "batch" / REPORT_KEY).exists()


def test_a_job_where_every_item_fails_is_failed_and_the_failure_list_is_capped(world):
    payload, _, _ = world
    rows = [{"path": f"p/{i}", "ok": False, "predictions": None, "code": "bad_input", "message": "x"} for i in range(MAX_REPORT_FAILURES + 3)]
    report = predict_batch.build_report(payload, rows)
    assert (report["status"], report["code"]) == ("failed", predict_batch.ItemCode.ALL_ITEMS_FAILED)
    assert len(report["failures"]) == MAX_REPORT_FAILURES and report["failures_truncated"]
    assert report["counts"]["failed"] == MAX_REPORT_FAILURES + 3


def test_an_empty_dataset_succeeds_with_zero_counts(world, tmp_path):
    payload, _, root = world
    pq.write_table(pa.table({"path": pa.array([], pa.string())}), root / "index.parquet")
    report = predict_batch.run(payload, DirStore(str(tmp_path / "out"), "batch/"), predict=pytest.fail)
    assert (report["status"], report["counts"]["total"]) == ("succeeded", 0)


def test_the_envelope_is_checked():
    with pytest.raises(ValueError, match="kind"):
        parse_spec(json.dumps({"spec_version": 1, "kind": "train", "payload": {}}))
    with pytest.raises(ValueError, match="version"):
        parse_spec(json.dumps({"spec_version": 2, "kind": "predict_batch", "payload": {}}))


def test_on_ray_the_same_rows_come_back(world):
    pytest.importorskip("ray.data")
    import ray

    payload, _, _ = world
    ray.init(num_cpus=2, include_dashboard=False, ignore_reinit_error=True)
    try:
        items = predict_batch.list_items(payload["data"])
        assert sorted(predict_batch.ray_predict(payload, items), key=lambda r: r["path"]) == sorted(local_predict(payload, items), key=lambda r: r["path"])
    finally:
        ray.shutdown()
