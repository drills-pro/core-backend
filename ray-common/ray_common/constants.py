"""Values every Ray package must agree on with core-api and with each other (core-api tests/integrations/test_runtime_drift.py
asserts them against core-api's own copies)."""
SPEC_VERSION = 1  # the job spec version the packages read (core-api app/integrations/job_spec.py)
REPORT_KEY = "report.json"  # under a job's prefix (core-api layout.REPORT_KEY)
SMOKE_KEY = "smoke.json"  # written by a smoke job: {"ok": bool} (core-api layout.SMOKE_KEY)
REPORT_SCHEMA_VERSION = 1  # schema_version of every report.json
HTTP_TIMEOUT_SECONDS = 60

LOCAL_SCHEME = "file://"  # STAGING_ENDPOINT=file:///dir writes under /dir/<prefix>: local runs and CI, no S3 needed
# boto3 >= 1.36 signs a checksum trailer into every PutObject unless told otherwise; the staging store (S3-compatible, not AWS)
# answers with SignatureDoesNotMatch. core-api sets these in the job's env too (job_submit.S3_COMPATIBLE_ENV).
CHECKSUM_ENV = ("AWS_REQUEST_CHECKSUM_CALCULATION", "AWS_RESPONSE_CHECKSUM_VALIDATION")
CHECKSUM_WHEN_REQUIRED = "when_required"
