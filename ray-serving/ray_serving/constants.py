SMOKE_KEY = "smoke.json"  # written under the job's prefix: {"ok": bool} (core-api data_plane/layout.py SMOKE_KEY)

# --- predict_batch (core-api app/integrations/job_spec.py BatchPredictSpec; tests/integrations/test_batch_predict_contract.py asserts equality) ---
SPEC_VERSION = 1
PREDICT_BATCH_KIND = "predict_batch"
PREDICTIONS_KEY = "predictions.jsonl"  # under the job's prefix: one line per item, {path, ok, predictions, code, message}
REPORT_KEY = "report.json"
REPORT_SCHEMA_VERSION = 1
MAX_REPORT_FAILURES = 200  # a report lists at most this many failed items (the counts are always complete)
HTTP_TIMEOUT_SECONDS = 60
