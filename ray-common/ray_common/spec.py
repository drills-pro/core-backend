"""The job spec envelope (core-api app/integrations/job_spec.py): the one check every package runs before it reads a payload."""
import json

from ray_common.constants import SPEC_VERSION
from ray_common.enums import JobKind


def parse_envelope(text: str, kind: JobKind, runner: str) -> dict:
    """The spec document, once its version is the one this code reads and its kind is the one `runner` runs."""
    doc = json.loads(text)
    if doc.get("spec_version") != SPEC_VERSION:
        raise ValueError(f"job spec version {doc.get('spec_version')!r} not supported, expected {SPEC_VERSION}")
    if doc.get("kind") != kind:
        raise ValueError(f"job kind {doc.get('kind')!r} is not run by {runner}")
    return doc
