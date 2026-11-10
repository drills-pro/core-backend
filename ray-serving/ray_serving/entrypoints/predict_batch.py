"""Batch inference job (`kind: predict_batch`, docs/ray-parallelism.md): predict every image of a dataset version with one model.

    python -m ray_serving.entrypoints.predict_batch '<job spec json>'      # STAGING_* in the environment

Reads the index and the object manifest from the spec's presigned urls, runs a Ray Data `map_batches` whose actors each load the model
once through `ray_serving.model.load` and answer each item through `model.predict` (so a batch answer equals a live one), and writes
`predictions.jsonl` and `report.json` under the job's prefix. One bad item is a failure line in the report, never the end of the job;
report.json is written in a `finally`, whatever happened. The actors fetch the images, the driver only lists them.
"""
import base64
import io
import json
import sys
from enum import StrEnum
from typing import Callable

from ray_serving import model
from ray_serving.batch_spec import INDEX_PATH_COLUMN, allowed_base, parse_spec
from ray_serving.constants import MAX_REPORT_FAILURES, PREDICTIONS_KEY, REPORT_KEY, REPORT_SCHEMA_VERSION
from ray_serving.store import http_get, store_from_env

OK, SUCCEEDED, FAILED = "ok", "succeeded", "failed"


class ItemCode(StrEnum):
    """`code` of a failed item or job; an item may also carry a replica code (model.ErrorCode)."""

    MISSING_OBJECT = "missing_object"  # the index lists a path the manifest has no url for
    FETCH_FAILED = "fetch_failed"
    INTERNAL = "internal_error"
    ALL_ITEMS_FAILED = "all_items_failed"


Fetch = Callable[[str], bytes]


def list_items(data: dict, fetch: Fetch = http_get) -> list[dict]:
    """[{path, url}] for every row of the index; `url` is "" when the manifest has none for it."""
    import pyarrow.parquet as pq

    paths = pq.read_table(io.BytesIO(fetch(data["index_url"])), columns=[INDEX_PATH_COLUMN]).column(INDEX_PATH_COLUMN).to_pylist()
    urls = json.loads(fetch(data["manifest_url"]))
    return [{"path": p, "url": urls.get(p, "")} for p in paths]


def predict_item(loaded: model.Loaded, item: dict, fetch: Fetch) -> dict:
    row = {"path": item["path"], OK: False, "predictions": None, "code": None, "message": None}
    if not item["url"]:
        return {**row, "code": ItemCode.MISSING_OBJECT, "message": "the manifest has no url for this path"}
    try:
        image = fetch(item["url"])
    except Exception as e:  # noqa: BLE001 - an unreadable object is this item's failure
        return {**row, "code": ItemCode.FETCH_FAILED, "message": f"{type(e).__name__}: {e}"}
    try:
        return {**row, OK: True, "predictions": model.predict(loaded, {"image_base64": base64.b64encode(image).decode()})}
    except model.ServeError as e:
        return {**row, "code": e.code, "message": str(e)}
    except Exception as e:  # noqa: BLE001 - the model failed on this item
        return {**row, "code": ItemCode.INTERNAL, "message": f"{type(e).__name__}: {e}"}


def predict_items(loaded: model.Loaded, items: list[dict], fetch: Fetch = http_get) -> list[dict]:
    """One result per item, in order: {path, ok, predictions, code, message}. Never raises for an item."""
    return [predict_item(loaded, item, fetch) for item in items]


class BatchPredictor:
    """The Ray Data actor: loads the model once in `__init__`, then answers each batch of {path, url} columns."""

    def __init__(self, spec: dict, base: str):
        self.loaded = model.load(model.ModelSpec.from_dict(spec), base)

    def __call__(self, batch: dict) -> dict:
        rows = predict_items(self.loaded, [{"path": str(p), "url": str(u)} for p, u in zip(batch["path"], batch["url"], strict=True)])
        # ray.data columns are typed arrays: predictions travel as JSON text and None as ""
        return {"path": [r["path"] for r in rows], OK: [r[OK] for r in rows], "predictions": [json.dumps(r["predictions"]) for r in rows],
                "code": [r["code"] or "" for r in rows], "message": [r["message"] or "" for r in rows]}


def ray_predict(payload: dict, items: list[dict]) -> list[dict]:
    """Predict `items` on a Ray Data actor pool of `payload["concurrency"]` models; same row shape as predict_items."""
    import ray.data

    spec = payload["model"]
    done = ray.data.from_items(items).map_batches(
        BatchPredictor, batch_size=payload["batch_size"], concurrency=payload["concurrency"], batch_format="numpy",
        fn_constructor_kwargs={"spec": spec, "base": allowed_base(spec["model_url"])}).take_all()
    return [{"path": str(r["path"]), OK: bool(r[OK]), "predictions": json.loads(r["predictions"]) if r[OK] else None,
             "code": str(r["code"]) or None, "message": str(r["message"]) or None} for r in done]


def _base(payload: dict | None) -> dict:
    data = (payload or {}).get("data") or {}
    return {"schema_version": REPORT_SCHEMA_VERSION, "dataset": {"version_id": data.get("version_id"), "lakefs_commit": data.get("lakefs_commit")}}


def build_report(payload: dict, rows: list[dict]) -> dict:
    failures = [{k: r[k] for k in ("path", "code", "message")} for r in rows if not r[OK]]
    counts = {"total": len(rows), SUCCEEDED: len(rows) - len(failures), FAILED: len(failures)}
    dead = bool(rows) and not counts[SUCCEEDED]
    return {**_base(payload), "status": FAILED if dead else SUCCEEDED, "code": ItemCode.ALL_ITEMS_FAILED if dead else None,
            "message": "no item could be predicted" if dead else None, "counts": counts,
            "failures": failures[:MAX_REPORT_FAILURES], "failures_truncated": len(failures) > MAX_REPORT_FAILURES}


def failed_report(payload: dict | None, e: BaseException) -> dict:
    return {**_base(payload), "status": FAILED, "code": ItemCode.INTERNAL, "message": f"{type(e).__name__}: {e}",
            "counts": {"total": 0, SUCCEEDED: 0, FAILED: 0}, "failures": [], "failures_truncated": False}


def run(payload: dict, store, predict=ray_predict, fetch: Fetch = http_get) -> dict:
    """Predict the dataset and write predictions.jsonl, then report.json (always). `predict(payload, items)` is the Ray part."""
    report = failed_report(payload, RuntimeError("the job did not finish"))
    try:
        items = list_items(payload["data"], fetch)
        rows = sorted(predict(payload, items) if items else [], key=lambda r: r["path"])
        store.put(PREDICTIONS_KEY, "".join(json.dumps(r) + "\n" for r in rows).encode())
        report = build_report(payload, rows)
    except Exception as e:  # noqa: BLE001 - the report must always be written
        report = failed_report(payload, e)
    finally:
        store.put(REPORT_KEY, json.dumps(report, default=str).encode())
    return report


def main(argv: list[str]) -> int:
    """Exit code 1 when the job failed. A spec this job cannot read has no prefix to report to, so it raises."""
    payload = parse_spec(argv[1])
    return 1 if run(payload, store_from_env(payload["bucket"], payload["prefix"]))["status"] == FAILED else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
