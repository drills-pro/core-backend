"""The job spec document (core-api app/integrations/job_spec.py; JSON Schema in contracts/job-spec.v1.json).

This package runs `ingest` jobs and reads only what it needs from the envelope: it refuses a spec version or kind
it does not know, and passes the payload on as the run's params.
"""
import json

from ray_jobs.common.constants import SPEC_VERSION
from ray_jobs.common.enums import JobKind


def parse_spec(text: str) -> dict:
    spec = json.loads(text)
    if spec.get("spec_version") != SPEC_VERSION:
        raise ValueError(f"job spec version {spec.get('spec_version')!r} not supported, expected {SPEC_VERSION}")
    if spec.get("kind") != JobKind.INGEST:
        raise ValueError(f"job kind {spec.get('kind')!r} is not run by ray_jobs")
    return spec["payload"]
