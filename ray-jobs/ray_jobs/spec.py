"""The job spec document (core-api app/integrations/job_spec.py; JSON Schema in contracts/job-spec.v1.json).

This package runs `ingest` jobs and reads only what it needs from the envelope: it refuses a spec version or kind
it does not know (ray_common.spec), and passes the payload on as the run's params.
"""
from ray_common.spec import parse_envelope
from ray_jobs.common.enums import JobKind


def parse_spec(text: str) -> dict:
    return parse_envelope(text, JobKind.INGEST, "ray_jobs")["payload"]
