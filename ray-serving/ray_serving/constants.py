from ray_common.constants import HTTP_TIMEOUT_SECONDS, REPORT_KEY, REPORT_SCHEMA_VERSION, SMOKE_KEY, SPEC_VERSION  # noqa: F401 - shared with the other Ray packages
from ray_common.enums import JobKind

# --- predict_batch (core-api app/integrations/job_spec.py BatchPredictSpec; tests/integrations/test_batch_predict_contract.py asserts equality) ---
PREDICT_BATCH_KIND = JobKind.PREDICT_BATCH
PREDICTIONS_KEY = "predictions.jsonl"  # under the job's prefix: one line per item, {path, ok, predictions, code, message}
MAX_REPORT_FAILURES = 200  # a report lists at most this many failed items (the counts are always complete)
